#!/usr/bin/env python3
"""
web_server.py — a browser front end for chess_arena.

    python web_server.py                  # http://127.0.0.1:8000
    python web_server.py --port 9000 --mock

Standard library only (plus what chess_arena already needs). The game runs in a
background thread and pushes events to the browser over Server-Sent Events, so
you can open the page mid-game, or in several tabs, and everyone sees the same
board.

chess_arena stays the source of truth: the engines, the prompt, the move parser
and the PGN writer are all imported from it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import queue
import random
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import chess

import chess_arena as arena

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
STATIC = WEB / "static"

ENGINE_CHOICES = ("claude", "gemini", "mock")


# --- Event hub --------------------------------------------------------------

class Hub:
    """Fan-out of game events to every connected browser, plus the transcript
    of the current game so a late arrival can catch up."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self.snapshot: dict = idle_snapshot()

    def publish(self, event: dict) -> None:
        with self._lock:
            apply_event(self.snapshot, event)
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.discard(q)

    def subscribe(self) -> tuple[queue.Queue, dict]:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.add(q)
            return q, json.loads(json.dumps(self.snapshot))

    def state(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self.snapshot))

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def reset(self, snapshot: dict) -> None:
        with self._lock:
            self.snapshot = snapshot


def idle_snapshot() -> dict:
    return {
        "status": "idle",          # idle | running | finished | error
        "white": None,
        "black": None,
        "fen": chess.Board().fen(),
        "moves": [],
        "log": [],
        "thinking": None,
        "result": None,
        "termination": None,
        "saved": None,
        "pgn": None,
        "started": None,
    }


def apply_event(snap: dict, ev: dict) -> None:
    """Fold an event into the snapshot, so /api/state and a fresh SSE
    connection always describe the same game the live clients are watching."""
    kind = ev["type"]
    if kind == "start":
        snap.update(status="running", white=ev["white"], black=ev["black"],
                    fen=ev["fen"], moves=[], log=[], thinking=None,
                    result=None, termination=None, saved=None, pgn=None,
                    started=ev["at"])
    elif kind == "thinking":
        snap["thinking"] = {"color": ev["color"], "engine": ev["engine"],
                            "since": ev["at"]}
    elif kind == "move":
        snap["moves"].append(ev["move"])
        snap["fen"] = ev["move"]["fen_after"]
        snap["thinking"] = None
    elif kind == "gameover":
        snap.update(status="finished", result=ev["result"],
                    termination=ev["termination"], pgn=ev.get("pgn"),
                    saved=ev.get("saved"), thinking=None)
    elif kind == "error":
        snap.update(status="error", thinking=None)
    if kind in ("log", "error"):
        snap["log"].append(ev)
        del snap["log"][:-200]


# --- The game, driven for the web -------------------------------------------

class Session:
    """One game at a time. Start it, watch it, stop it."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, config: dict) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("a game is already running")
            self.stop_flag = threading.Event()
            self.hub.reset(idle_snapshot())
            self.thread = threading.Thread(target=self._run, args=(config,),
                                           daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stop_flag.set()

    # -- internals --

    def _engine(self, kind: str, config: dict, seat: str) -> arena.Engine:
        if kind == "mock":
            # Label it by seat, so two mock engines don't share a name.
            return arena.MockEngine(f"Mock {seat[0].upper()}")
        if kind == "claude":
            require_key("ANTHROPIC_API_KEY")
            return arena.ClaudeEngine(config.get("claude_model")
                                      or arena.CLAUDE_MODEL)
        if kind == "gemini":
            require_key("GEMINI_API_KEY")
            return arena.GeminiEngine(config.get("gemini_model")
                                      or arena.GEMINI_MODEL)
        raise ValueError(f"unknown engine {kind!r}")

    def _log(self, level: str, text: str, **extra) -> None:
        self.hub.publish({"type": "log", "level": level, "text": text,
                          "at": now(), **extra})

    def _run(self, config: dict) -> None:
        try:
            white = self._engine(config["white"], config, "white")
            black = self._engine(config["black"], config, "black")
        except Exception as exc:                                # noqa: BLE001
            self.hub.publish({"type": "error", "at": now(),
                              "level": "error",
                              "text": f"Could not start the engines: {exc}"})
            return

        delay = float(config.get("delay", 1.0))
        max_plies = int(config.get("max_plies", arena.MAX_PLIES))
        forfeit = bool(config.get("forfeit_on_illegal", False))
        retries = int(config.get("retries", arena.MAX_RETRIES))

        board = chess.Board()
        record = {
            "started": dt.datetime.now().isoformat(timespec="seconds"),
            "white": {"label": white.label, "model": white.model},
            "black": {"label": black.label, "model": black.model},
            "moves": [],
            "result": None,
            "termination": None,
        }

        self.hub.publish({
            "type": "start", "at": now(),
            "white": {"label": white.label, "model": white.model,
                      "kind": config["white"]},
            "black": {"label": black.label, "model": black.model,
                      "kind": config["black"]},
            "fen": board.fen(),
        })

        try:
            self._play(board, record, white, black, delay, max_plies,
                       forfeit, retries)
        except Exception:                                       # noqa: BLE001
            self.hub.publish({"type": "error", "at": now(), "level": "error",
                              "text": "The game loop crashed.",
                              "detail": traceback.format_exc()})
            return

        if record["result"] is None:
            outcome = board.outcome(claim_draw=True)
            if outcome:
                record["result"] = outcome.result()
                record["termination"] = \
                    outcome.termination.name.replace("_", " ").title()
            elif self.stop_flag.is_set():
                record["result"] = "*"
                record["termination"] = "Stopped by the operator"
            else:
                record["result"] = "*"
                record["termination"] = f"Stopped at the {max_plies}-ply limit"

        # arena.to_pgn hard-codes the Event name; the seats are configurable
        # here, so name the event after whoever actually played.
        record["pgn"] = arena.to_pgn(board, record).replace(
            '[Event "Claude vs Gemini"]',
            f'[Event "{white.label} vs {black.label}"]', 1)
        saved = None
        if record["moves"]:
            try:
                saved = arena.save(record).name
            except Exception as exc:                            # noqa: BLE001
                self._log("warn", f"Could not save the game: {exc}")

        self.hub.publish({"type": "gameover", "at": now(),
                          "result": record["result"],
                          "termination": record["termination"],
                          "pgn": record["pgn"], "saved": saved})

    def _play(self, board, record, white, black, delay, max_plies,
              forfeit, retries) -> None:
        while (not board.is_game_over(claim_draw=True)
               and len(board.move_stack) < max_plies
               and not self.stop_flag.is_set()):

            engine = white if board.turn == chess.WHITE else black
            color = "white" if board.turn == chess.WHITE else "black"
            fen_before = board.fen()

            self.hub.publish({"type": "thinking", "at": now(),
                              "color": color, "engine": engine.label})

            move = None
            complaint = None
            attempts = []
            started = time.time()

            for attempt in range(retries):
                if self.stop_flag.is_set():
                    return
                prompt = arena.build_prompt(board, complaint)
                try:
                    raw = engine.ask(prompt)
                except Exception as exc:                        # noqa: BLE001
                    raw = ""
                    complaint = f"The API call failed ({exc.__class__.__name__})."
                    attempts.append({"raw": None, "error": str(exc)})
                    self._log("error",
                              f"{engine.label}: {describe_api_error(exc)}",
                              color=color, detail=str(exc)[:400])
                    time.sleep(2 ** attempt)
                    continue

                move = arena.extract_move(raw, board)
                attempts.append({"raw": raw, "accepted": move is not None})
                if move:
                    break
                complaint = (f"'{raw[:60]}' is not a legal move in this position."
                             if raw else "You returned an empty response.")
                self._log("warn",
                          f"{engine.label} offered an illegal move "
                          f"({attempt + 1}/{retries}): "
                          f"{(raw[:60] or '(empty response)')!r}",
                          color=color)

            fallback = False
            if move is None:
                # Say which it was: the model kept answering with non-moves, or
                # the API never answered at all.
                why = ("failed API calls"
                       if all(a.get("error") for a in attempts) else "illegal moves")
                if forfeit:
                    record["result"] = "0-1" if board.turn == chess.WHITE else "1-0"
                    record["termination"] = f"{engine.label} forfeited ({why})"
                    self._log("error",
                              f"{engine.label} forfeits after {retries} "
                              f"{why}.", color=color)
                    return
                move = random.choice(list(board.legal_moves))
                fallback = True
                self._log("warn",
                          f"Falling back to a random legal move for "
                          f"{engine.label}: {board.san(move)}", color=color)

            san = board.san(move)
            elapsed = time.time() - started
            board.push(move)

            entry = {
                "ply": len(board.move_stack),
                "color": "White" if color == "white" else "Black",
                "engine": engine.label,
                "uci": move.uci(),
                "san": san,
                "fen_before": fen_before,
                "fen_after": board.fen(),
                "seconds": round(elapsed, 2),
                "attempts": attempts,
                "fallback": fallback,
            }
            record["moves"].append(entry)

            self.hub.publish({
                "type": "move", "at": now(),
                "move": {**entry,
                         "from": move.uci()[:2],
                         "to": move.uci()[2:4],
                         "check": board.is_check(),
                         "retries": max(0, len(attempts) - 1)},
            })

            # Sleep in slices so Stop feels immediate.
            deadline = time.time() + delay
            while time.time() < deadline and not self.stop_flag.is_set():
                time.sleep(min(0.1, deadline - time.time()))


def now() -> str:
    return dt.datetime.now().isoformat(timespec="milliseconds")


def require_key(name: str) -> None:
    if not os.environ.get(name):
        raise RuntimeError(
            f"{name} is not set. Put it in {arena.HERE / '.env'} "
            f"(see .env.example) or export it before starting the server.")


def describe_api_error(exc: Exception) -> str:
    """A one-line reading of a provider error, because the raw JSON buries the
    part you need. The full text still goes to the log entry's detail."""
    text = str(exc)
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        return ("rate limited or out of quota — a whole game needs dozens of "
                "calls, so a small free-tier allowance will not cover one. "
                "Try another model, or enable billing on the project.")
    if "not_found_error" in text or "NOT_FOUND" in text or "404" in text:
        return "that model name was not found on this key."
    if "401" in text or "PERMISSION_DENIED" in text or "API key" in text:
        return "the API key was rejected."
    return f"API call failed — {text[:200]}"


# --- HTTP -------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ChessArena"

    # Wired up in main().
    hub: Hub
    session: Session
    defaults: dict

    def finish(self):
        # A browser closing an event stream makes the final flush fail; that is
        # normal, and not worth a traceback on the console.
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):       # quieter than the default
        if self.path.startswith("/api/") and not self.path.startswith("/api/events"):
            super().log_message(fmt, *args)

    # -- helpers --

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routes --

    def do_GET(self) -> None:                                   # noqa: N802
        route = urlparse(self.path).path

        if route in ("/", "/index.html"):
            return self._file(WEB / "index.html")

        if route.startswith("/static/"):
            rel = route[len("/static/"):]
            target = (STATIC / rel).resolve()
            if STATIC.resolve() in target.parents and target.is_file():
                return self._file(target)
            return self._json({"error": "not found"}, 404)

        if route == "/api/config":
            return self._json({
                "engines": ENGINE_CHOICES,
                "claude_model": arena.CLAUDE_MODEL,
                "gemini_model": arena.GEMINI_MODEL,
                "defaults": self.defaults,
            })

        if route == "/api/state":
            return self._json(self.hub.state())

        if route == "/api/events":
            return self._events()

        if route == "/api/games":
            return self._json({"games": list_games()})

        if route.startswith("/api/games/"):
            name = route[len("/api/games/"):]
            return self._game(name)

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:                                  # noqa: N802
        route = urlparse(self.path).path

        if route == "/api/game":
            config = {**self.defaults, **self._body()}
            for seat in ("white", "black"):
                if config.get(seat) not in ENGINE_CHOICES:
                    return self._json(
                        {"error": f"{seat} must be one of "
                                  f"{', '.join(ENGINE_CHOICES)}"}, 400)
            try:
                self.session.start(config)
            except RuntimeError as exc:
                return self._json({"error": str(exc)}, 409)
            return self._json({"ok": True})

        if route == "/api/stop":
            self.session.stop()
            return self._json({"ok": True})

        self._json({"error": "not found"}, 404)

    # -- SSE --

    def _events(self) -> None:
        q, snapshot = self.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        # No Content-Length and no chunked framing: the stream ends when the
        # connection closes, which is what EventSource expects.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self._send_event({"type": "snapshot", **snapshot})
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                self._send_event(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)

    def _send_event(self, event: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
        self.wfile.flush()

    # -- saved games --

    def _game(self, name: str) -> None:
        if "/" in name or "\\" in name or not name:
            return self._json({"error": "bad name"}, 400)
        path = (arena.GAMES_DIR / name).with_suffix(".json")
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        try:
            return self._json(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, 500)


def list_games() -> list[dict]:
    out = []
    for path in sorted(arena.GAMES_DIR.glob("game-*.json"), reverse=True):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "name": path.stem,
            "started": record.get("started"),
            "white": (record.get("white") or {}).get("label"),
            "black": (record.get("black") or {}).get("label"),
            "white_model": (record.get("white") or {}).get("model"),
            "black_model": (record.get("black") or {}).get("model"),
            "result": record.get("result"),
            "termination": record.get("termination"),
            "plies": len(record.get("moves") or []),
        })
    return out


# --- Entry point ------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Web front end for chess_arena.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--delay", type=float, default=1.0,
                   help="default seconds between moves (default 1.0)")
    p.add_argument("--mock", action="store_true",
                   help="default both seats to the random-move engine")
    p.add_argument("--open", action="store_true",
                   help="open a browser window on startup")
    args = p.parse_args()

    hub = Hub()
    Handler.hub = hub
    Handler.session = Session(hub)
    Handler.defaults = {
        "white": "mock" if args.mock else "claude",
        "black": "mock" if args.mock else "gemini",
        "claude_model": arena.CLAUDE_MODEL,
        "gemini_model": arena.GEMINI_MODEL,
        "delay": args.delay,
        "max_plies": arena.MAX_PLIES,
        "retries": arena.MAX_RETRIES,
        "forfeit_on_illegal": False,
    }

    arena.GAMES_DIR.mkdir(exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}"
    print(f"Chess arena on {url}   (ctrl-c to stop)")
    if args.open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
        Handler.session.stop()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
