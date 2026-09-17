#!/usr/bin/env python3
"""
chess_arena.py — Claude vs Gemini, hands-off.

    pip install chess anthropic google-genai
    export ANTHROPIC_API_KEY=
    export GEMINI_API_KEY=

    python chess_arena.py                 # play a game
    python chess_arena.py --mock          # dry run, no API calls, no keys needed
    python chess_arena.py --replay games/game-20260908-141233.json

The orchestrator owns the board. The models never track state; every turn they
get a fresh prompt containing the FEN, the move history, and the exact list of
legal moves in UCI. Anything they return that isn't on that list is rejected
and re-asked.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path

import chess
import chess.pgn

# --- Configuration ----------------------------------------------------------

# Model IDs move fast. Check https://docs.claude.com/en/docs/about-claude/models
# and https://ai.google.dev/gemini-api/docs/models if either of these 404s.
CLAUDE_MODEL = "claude-sonnet-5"   # "claude-opus-5" plays better and costs more
GEMINI_MODEL = "gemini-3.8-flash"

os.environ["ANTHROPIC_API_KEY"] = "...."
os.environ["GEMINI_API_KEY"] = "...."

MAX_RETRIES = 3        # illegal-move attempts before the fallback kicks in
MOVE_DELAY = 2.0       # seconds between moves, for effect
MAX_PLIES = 300        # hard stop so a shuffling draw can't run forever
GAMES_DIR = Path("games")

SYSTEM_PROMPT = (
    "You are a strong chess engine. You reply with exactly one move in UCI "
    "notation and nothing else — no explanation, no punctuation, no commentary. "
    "The move you give must be one of the legal moves listed in the prompt."
)

UCI_RE = re.compile(r"\b([a-h][1-8][a-h][1-8][qrbnQRBN]?)\b")


# --- Engines ----------------------------------------------------------------

class Engine(ABC):
    """Wraps a model behind one method: given a prompt, return raw text."""

    def __init__(self, label: str, model: str):
        self.label = label
        self.model = model

    @abstractmethod
    def ask(self, prompt: str) -> str:
        ...


class ClaudeEngine(Engine):
    def __init__(self, model: str = CLAUDE_MODEL):
        super().__init__("Claude", model)
        from anthropic import Anthropic
        self.client = Anthropic()  # reads ANTHROPIC_API_KEY

    def ask(self, prompt: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=64,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()


class GeminiEngine(Engine):
    def __init__(self, model: str = GEMINI_MODEL):
        super().__init__("Gemini", model)
        from google import genai
        from google.genai import types
        self._types = types
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def ask(self, prompt: str) -> str:
        resp = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=self._types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                # Generous, because reasoning tokens count against this and a
                # truncated response comes back as None.
                max_output_tokens=2048,
                temperature=0.7,
            ),
        )
        return (resp.text or "").strip()


class MockEngine(Engine):
    """Plays random legal moves. Occasionally returns garbage so you can watch
    the retry path work."""

    def __init__(self, label: str):
        super().__init__(label, "mock")

    def ask(self, prompt: str) -> str:
        legal = re.search(r"Legal moves \(UCI\):\n(.+)", prompt).group(1)
        moves = [m.strip() for m in legal.split(",")]
        if random.random() < 0.15:
            return random.choice(["I'll play Nf3!", "z9z9", "e2e4e2"])
        return random.choice(moves)


# --- Prompt and parsing -----------------------------------------------------

def history_san(board: chess.Board) -> str:
    """Move list in the '1. e4 e5 2. Nf3' form, replayed from the start."""
    replay = chess.Board()
    parts = []
    for i, move in enumerate(board.move_stack):
        if i % 2 == 0:
            parts.append(f"{i // 2 + 1}.")
        parts.append(replay.san(move))
        replay.push(move)
    return " ".join(parts) if parts else "(none — this is the opening move)"


def build_prompt(board: chess.Board, complaint: str | None = None) -> str:
    legal = sorted(m.uci() for m in board.legal_moves)
    color = "White" if board.turn == chess.WHITE else "Black"
    lines = [
        f"You are playing chess as {color}.",
        "",
        f"Position (FEN): {board.fen()}",
        f"Move {board.fullmove_number}, {color} to play."
        + ("  YOU ARE IN CHECK." if board.is_check() else ""),
        "",
        "Board (uppercase = White, lowercase = Black, rank 8 on top):",
        str(board),
        "",
        f"Moves so far: {history_san(board)}",
        "",
        "Legal moves (UCI):",
        ", ".join(legal),
        "",
        "Reply with exactly one move from that list, in UCI notation. "
        "Nothing else.",
    ]
    if complaint:
        lines += ["", f"NOTE: {complaint} Try again."]
    return "\n".join(lines)


def extract_move(text: str, board: chess.Board) -> chess.Move | None:
    """Pull a legal move out of whatever the model said, or return None."""
    legal = {m.uci(): m for m in board.legal_moves}

    # Exact UCI, the happy path.
    cleaned = text.strip().strip(".!,\"'` \n")
    if cleaned.lower() in legal:
        return legal[cleaned.lower()]

    # UCI buried in a sentence.
    for candidate in UCI_RE.findall(text):
        if candidate.lower() in legal:
            return legal[candidate.lower()]

    # It gave SAN instead. Accept it rather than burn a retry.
    for token in re.findall(r"[A-Za-z][A-Za-z0-9+#=\-]{1,6}", text):
        try:
            return board.parse_san(token)
        except ValueError:
            continue
    return None


# --- Display ----------------------------------------------------------------

def show(board: chess.Board, ascii_only: bool = False) -> None:
    print()
    if ascii_only:
        print(str(board))
    else:
        print(board.unicode(borders=True, invert_color=True, empty_square="·"))
    print()


def banner(text: str) -> None:
    print(f"\n{'=' * 58}\n{text}\n{'=' * 58}")


# --- The game loop ----------------------------------------------------------

def play(white: Engine, black: Engine, *, delay: float, max_plies: int,
         ascii_only: bool, forfeit_on_illegal: bool) -> dict:
    board = chess.Board()
    record = {
        "started": dt.datetime.now().isoformat(timespec="seconds"),
        "white": {"label": white.label, "model": white.model},
        "black": {"label": black.label, "model": black.model},
        "moves": [],
        "result": None,
        "termination": None,
    }

    banner(f"{white.label} ({white.model})  vs  {black.label} ({black.model})")
    show(board, ascii_only)

    while not board.is_game_over(claim_draw=True) and len(board.move_stack) < max_plies:
        engine = white if board.turn == chess.WHITE else black
        color = "White" if board.turn == chess.WHITE else "Black"
        fen_before = board.fen()

        move = None
        complaint = None
        attempts = []
        started = time.time()

        for attempt in range(MAX_RETRIES):
            prompt = build_prompt(board, complaint)
            try:
                raw = engine.ask(prompt)
            except Exception as exc:                      # noqa: BLE001
                raw = ""
                complaint = f"The API call failed ({exc.__class__.__name__})."
                attempts.append({"raw": None, "error": str(exc)})
                time.sleep(2 ** attempt)
                continue

            move = extract_move(raw, board)
            attempts.append({"raw": raw, "accepted": move is not None})
            if move:
                break
            complaint = (
                f"'{raw[:60]}' is not a legal move in this position."
                if raw else "You returned an empty response."
            )
            print(f"  ⚠  {engine.label} offered an illegal move "
                  f"({attempt + 1}/{MAX_RETRIES}): {raw[:60]!r}")

        fallback = False
        if move is None:
            if forfeit_on_illegal:
                record["result"] = "0-1" if board.turn == chess.WHITE else "1-0"
                record["termination"] = f"{engine.label} forfeited (illegal moves)"
                print(f"\n  {engine.label} forfeits after {MAX_RETRIES} illegal moves.")
                break
            move = random.choice(list(board.legal_moves))
            fallback = True
            print(f"  ⚠  Falling back to a random legal move for {engine.label}.")

        san = board.san(move)
        elapsed = time.time() - started
        board.push(move)

        record["moves"].append({
            "ply": len(board.move_stack),
            "color": color,
            "engine": engine.label,
            "uci": move.uci(),
            "san": san,
            "fen_before": fen_before,
            "fen_after": board.fen(),
            "seconds": round(elapsed, 2),
            "attempts": attempts,
            "fallback": fallback,
        })

        num = (len(board.move_stack) + 1) // 2
        dots = "." if color == "White" else "..."
        print(f"{num}{dots} {san:8s}  {engine.label:7s} ({elapsed:.1f}s)"
              + ("  [random fallback]" if fallback else ""))
        show(board, ascii_only)
        time.sleep(delay)

    if record["result"] is None:
        outcome = board.outcome(claim_draw=True)
        if outcome:
            record["result"] = outcome.result()
            record["termination"] = outcome.termination.name.replace("_", " ").title()
        else:
            record["result"] = "*"
            record["termination"] = f"Stopped at the {max_plies}-ply limit"

    banner(f"{record['result']}  —  {record['termination']}")
    record["pgn"] = to_pgn(board, record)
    return record


def to_pgn(board: chess.Board, record: dict) -> str:
    game = chess.pgn.Game.from_board(board)
    game.headers["Event"] = "Claude vs Gemini"
    game.headers["Date"] = record["started"][:10].replace("-", ".")
    game.headers["White"] = f"{record['white']['label']} ({record['white']['model']})"
    game.headers["Black"] = f"{record['black']['label']} ({record['black']['model']})"
    game.headers["Result"] = record["result"]
    game.headers["Termination"] = record["termination"] or "?"
    return str(game)


def save(record: dict) -> Path:
    GAMES_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = GAMES_DIR / f"game-{stamp}"
    base.with_suffix(".json").write_text(json.dumps(record, indent=2))
    base.with_suffix(".pgn").write_text(record["pgn"])
    return base


# --- Replay -----------------------------------------------------------------

def replay(path: Path, delay: float, ascii_only: bool) -> None:
    record = json.loads(path.read_text())
    board = chess.Board()

    banner(f"REPLAY — {record['white']['label']} vs {record['black']['label']}"
           f"  ({record['started']})")
    show(board, ascii_only)

    for entry in record["moves"]:
        board.push(chess.Move.from_uci(entry["uci"]))
        num = (entry["ply"] + 1) // 2
        dots = "." if entry["color"] == "White" else "..."
        print(f"{num}{dots} {entry['san']:8s}  {entry['engine']:7s} "
              f"({entry['seconds']}s)"
              + ("  [random fallback]" if entry.get("fallback") else ""))
        retries = len(entry.get("attempts", [])) - 1
        if retries > 0:
            print(f"     ({retries} illegal attempt{'s' if retries > 1 else ''} first)")
        show(board, ascii_only)
        time.sleep(delay)

    banner(f"{record['result']}  —  {record['termination']}")


# --- Entry point ------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Claude vs Gemini chess.")
    p.add_argument("--replay", type=Path, help="replay a saved game .json")
    p.add_argument("--delay", type=float, default=MOVE_DELAY,
                   help=f"seconds between moves (default {MOVE_DELAY})")
    p.add_argument("--mock", action="store_true",
                   help="random-move engines, no API calls")
    p.add_argument("--swap", action="store_true",
                   help="Gemini plays White (default: Claude plays White)")
    p.add_argument("--max-plies", type=int, default=MAX_PLIES)
    p.add_argument("--ascii", action="store_true",
                   help="letters instead of unicode pieces")
    p.add_argument("--forfeit-on-illegal", action="store_true",
                   help="lose the game instead of falling back to a random move")
    p.add_argument("--no-prompt", action="store_true",
                   help="start immediately instead of waiting for 'go'")
    args = p.parse_args()

    if args.replay:
        replay(args.replay, args.delay, args.ascii)
        return 0

    if args.mock:
        a, b = MockEngine("Claude"), MockEngine("Gemini")
    else:
        missing = [k for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY")
                   if not os.environ.get(k)]
        if missing:
            print(f"Missing environment variable(s): {', '.join(missing)}")
            return 1
        a, b = ClaudeEngine(), GeminiEngine()

    white, black = (b, a) if args.swap else (a, b)

    if not args.no_prompt:
        try:
            input("Type go and hit enter: ")
        except (EOFError, KeyboardInterrupt):
            return 130

    try:
        record = play(white, black, delay=args.delay, max_plies=args.max_plies,
                      ascii_only=args.ascii,
                      forfeit_on_illegal=args.forfeit_on_illegal)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

    base = save(record)
    print(f"Saved {base}.json and {base}.pgn")
    print(f"Replay it with:  python {Path(sys.argv[0]).name} --replay {base}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
