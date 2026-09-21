/* Chess Arena — front end.
 *
 * The server owns the game; this file owns the pixels. It keeps one "view"
 * object (either the live game or a replayed one from the archive) plus a
 * cursor into its move list, and repaints from that.
 */

const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const FILES = ["a", "b", "c", "d", "e", "f", "g", "h"];
const VALUE = { p: 1, n: 3, b: 3, r: 5, q: 9, k: 0 };
const ORDER = { q: 0, r: 1, b: 2, n: 3, p: 4, k: 5 };

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  live: emptyGame(),
  replay: null,          // a game loaded from the archive
  cursor: null,          // null = follow the live tip; otherwise a ply index
  flipped: false,
  autoplay: null,
  anim: null,            // {from, to} for the next repaint only
  config: null,
  veilOff: false,      // the result card has been clicked away
};

function emptyGame() {
  return {
    status: "idle", white: null, black: null, moves: [], log: [],
    thinking: null, result: null, termination: null, pgn: null, saved: null,
  };
}

const view = () => state.replay || state.live;
const isReplay = () => state.replay !== null;
const plyCount = () => view().moves.length;
const cursor = () => (state.cursor === null ? plyCount() : state.cursor);

/* --- position helpers --------------------------------------------------- */

function fenAt(game, ply) {
  if (ply <= 0) return START_FEN;
  return game.moves[ply - 1].fen_after;
}

function parseFen(fen) {
  const rows = fen.split(" ")[0].split("/");
  const grid = [];
  for (const row of rows) {
    const line = [];
    for (const ch of row) {
      if (ch >= "1" && ch <= "8") line.push(...Array(+ch).fill(null));
      else line.push(ch);
    }
    grid.push(line);
  }
  return grid;                               // grid[0] is rank 8
}

function pieceSrc(ch) {
  const light = ch === ch.toUpperCase();
  return `/static/pieces/${ch.toLowerCase()}${light ? "l" : "d"}t.svg`;
}

function squareName(rank, file) {            // rank 0 = rank 8
  return FILES[file] + (8 - rank);
}

function kingSquare(fen) {
  const grid = parseFen(fen);
  const white = fen.split(" ")[1] === "w";
  const king = white ? "K" : "k";
  for (let r = 0; r < 8; r++) {
    for (let f = 0; f < 8; f++) if (grid[r][f] === king) return squareName(r, f);
  }
  return null;
}

/* Material still on the board, so captures can be shown as what's missing. */
function census(fen) {
  const counts = { white: {}, black: {} };
  for (const ch of fen.split(" ")[0]) {
    if (!/[a-zA-Z]/.test(ch)) continue;
    const side = ch === ch.toUpperCase() ? "white" : "black";
    const type = ch.toLowerCase();
    counts[side][type] = (counts[side][type] || 0) + 1;
  }
  return counts;
}

const FULL_SET = { p: 8, n: 2, b: 2, r: 2, q: 1, k: 1 };

function capturedFrom(fen, side) {
  const have = census(fen)[side];
  const lost = [];
  for (const [type, n] of Object.entries(FULL_SET)) {
    const missing = n - (have[type] || 0);
    for (let i = 0; i < missing; i++) lost.push(type);
  }
  return lost.sort((a, b) => ORDER[a] - ORDER[b]);
}

function materialScore(fen) {
  const c = census(fen);
  const sum = (side) => Object.entries(c[side])
    .reduce((t, [type, n]) => t + VALUE[type] * n, 0);
  return sum("white") - sum("black");
}

/* --- board -------------------------------------------------------------- */

const boardEl = $("#board");

function renderBoard() {
  const game = view();
  const ply = cursor();
  const fen = fenAt(game, ply);
  const move = ply > 0 ? game.moves[ply - 1] : null;
  const grid = parseFen(fen);

  const from = move ? move.uci.slice(0, 2) : null;
  const to = move ? move.uci.slice(2, 4) : null;
  const inCheck = move && /[+#]/.test(move.san);
  const checkSq = inCheck ? kingSquare(fen) : null;

  const sq = boardEl.clientWidth / 8;
  const anim = state.anim;
  state.anim = null;

  const ranks = state.flipped ? [7, 6, 5, 4, 3, 2, 1, 0] : [0, 1, 2, 3, 4, 5, 6, 7];
  const files = state.flipped ? [7, 6, 5, 4, 3, 2, 1, 0] : [0, 1, 2, 3, 4, 5, 6, 7];

  const frag = document.createDocumentFragment();
  ranks.forEach((r, rowIdx) => {
    files.forEach((f, colIdx) => {
      const name = squareName(r, f);
      const cell = document.createElement("div");
      cell.className = "sq" + ((r + f) % 2 ? " dark" : "");
      if (name === from || name === to) cell.classList.add("hl");
      if (name === checkSq) cell.classList.add("check");

      if (colIdx === 0) cell.append(coord("rank", String(8 - r)));
      if (rowIdx === 7) cell.append(coord("file", FILES[f]));

      const piece = grid[r][f];
      if (piece) {
        const img = document.createElement("img");
        img.className = "piece";
        img.src = pieceSrc(piece);
        img.alt = piece;
        if (anim && name === anim.to && sq) {
          const [fx, fy] = displayXY(anim.from);
          const [tx, ty] = displayXY(anim.to);
          img.style.setProperty("--dx", `${(fx - tx) * sq}px`);
          img.style.setProperty("--dy", `${(fy - ty) * sq}px`);
          img.classList.add("slide");
        }
        cell.append(img);
      }
      frag.append(cell);
    });
  });

  boardEl.replaceChildren(frag);
}

function coord(kind, text) {
  const el = document.createElement("span");
  el.className = `coord ${kind}`;
  el.textContent = text;
  return el;
}

/* Column/row of a square as currently displayed, in board units. */
function displayXY(name) {
  const file = FILES.indexOf(name[0]);
  const rank = 8 - Number(name[1]);          // 0 = rank 8
  return state.flipped ? [7 - file, 7 - rank] : [file, rank];
}

/* --- player cards ------------------------------------------------------- */

function renderCards() {
  const game = view();
  const fen = fenAt(game, cursor());
  const score = materialScore(fen);
  const turn = fen.split(" ")[1] === "w" ? "white" : "black";

  const seats = state.flipped
    ? { "#card-top": "white", "#card-bottom": "black" }
    : { "#card-top": "black", "#card-bottom": "white" };

  for (const [sel, side] of Object.entries(seats)) {
    const card = $(sel);
    const player = game[side];
    card.dataset.side = side;

    $(".label", card).textContent = player ? player.label : (side === "white" ? "White" : "Black");
    $(".model", card).textContent = player ? player.model : "";

    // This runs on the clock tick too, so only touch the DOM when the set of
    // captured pieces has actually changed.
    const caps = capturedFrom(fen, side === "white" ? "black" : "white");
    const capsEl = $(".captures", card);
    const key = side + ":" + caps.join("");   // side matters: flipping swaps the card
    if (capsEl.dataset.key !== key) {
      capsEl.dataset.key = key;
      capsEl.replaceChildren(...caps.map((type) => {
        const img = document.createElement("img");
        img.src = pieceSrc(side === "white" ? type : type.toUpperCase());
        img.alt = type;
        return img;
      }));
    }

    const edge = side === "white" ? score : -score;
    $(".material", card).textContent = edge > 0 ? `+${edge}` : "";

    const seconds = game.moves
      .slice(0, cursor())
      .filter((m) => m.color.toLowerCase() === side)
      .reduce((t, m) => t + (m.seconds || 0), 0);
    $(".clock", card).textContent = formatClock(seconds, side);

    const thinking = !isReplay() && game.thinking && game.thinking.color === side;
    card.classList.toggle("is-thinking", Boolean(thinking));
    card.classList.toggle("is-active",
      Boolean(thinking) || (game.status === "running" && turn === side));
  }
}

function formatClock(seconds, side) {
  const live = !isReplay() && view().thinking && view().thinking.color === side;
  if (live) {
    const since = new Date(view().thinking.since).getTime();
    const elapsed = (Date.now() - since) / 1000;
    return `${(seconds + elapsed).toFixed(1)}s`;
  }
  return `${seconds.toFixed(1)}s`;
}

/* --- move list, log, pgn ------------------------------------------------ */

function renderMoves() {
  const game = view();
  const list = $("#movelist");
  if (!game.moves.length) {
    list.replaceChildren(el("p", "empty", "No moves yet."));
    return;
  }
  const frag = document.createDocumentFragment();
  for (let i = 0; i < game.moves.length; i += 2) {
    const row = el("div", "mv-row");
    row.append(el("span", "mv-no", String(i / 2 + 1)));
    row.append(moveButton(game.moves[i], i + 1));
    if (game.moves[i + 1]) row.append(moveButton(game.moves[i + 1], i + 2));
    else row.append(el("span", ""));
    frag.append(row);
  }
  list.replaceChildren(frag);
  const active = $(".mv.is-on", list);
  if (active) active.scrollIntoView({ block: "nearest" });
}

function moveButton(move, ply) {
  const btn = el("button", "mv", move.san);
  if (ply === cursor()) btn.classList.add("is-on");
  const retries = move.retries ?? Math.max(0, (move.attempts || []).length - 1);
  if (move.fallback) btn.append(el("span", "flag fallback"));
  else if (retries > 0) btn.append(el("span", "flag retry"));
  btn.title = `${move.engine} · ${move.seconds}s`
    + (retries ? ` · ${retries} illegal attempt${retries > 1 ? "s" : ""}` : "")
    + (move.fallback ? " · random fallback" : "");
  btn.onclick = () => goto(ply);
  return btn;
}

function renderLog() {
  const box = $("#log");
  const lines = state.live.log;
  if (!lines.length) {
    box.replaceChildren(el("p", "empty", "Retries, API errors and forfeits show up here."));
    return;
  }
  box.replaceChildren(...lines.map((entry) => {
    const line = el("div", `log-line ${entry.level || "info"}`);
    line.append(el("time", "", new Date(entry.at).toLocaleTimeString()));
    line.append(el("span", "body", entry.text + (entry.detail ? `\n${entry.detail}` : "")));
    return line;
  }));
  box.scrollTop = box.scrollHeight;
}

function renderPgn() {
  const game = view();
  $("#pgn").textContent = game.pgn || (game.moves.length
    ? "The PGN appears when the game ends."
    : "No game yet.");
}

/* --- status, viewer ----------------------------------------------------- */

function renderStatus() {
  const game = view();
  const chip = $("#status-chip");
  const label = isReplay() ? "replay" : game.status;
  chip.textContent = label;
  chip.dataset.state = isReplay() ? "finished" : game.status;

  const running = state.live.status === "running";
  $("#start").disabled = running;
  $("#stop").disabled = !running;
  $("#back-to-live").hidden = !isReplay();

  const matchup = game.white && game.black
    ? `${game.white.label} vs ${game.black.label}`
    : "model versus model, hands off";
  $("#matchup").textContent = matchup;

  const veil = $("#veil");
  const atEnd = cursor() === plyCount() && plyCount() > 0;
  if (game.result && atEnd && !state.veilOff) {
    $("#veil-result").textContent = { "1-0": "1 – 0", "0-1": "0 – 1", "1/2-1/2": "½ – ½" }[game.result]
      || "no result";
    $("#veil-term").textContent = game.termination || "";
    veil.hidden = false;
  } else {
    veil.hidden = true;
  }

  $("#viewer-pos").textContent = `move ${Math.ceil(cursor() / 2)} of ${Math.ceil(plyCount() / 2)}`;
  $("[data-nav=play]").textContent = state.autoplay ? "❚❚" : "▶";
}

function render() {
  renderBoard();
  renderCards();
  renderMoves();
  renderPgn();
  renderStatus();
}

/* --- navigation --------------------------------------------------------- */

function goto(ply) {
  const max = plyCount();
  const next = Math.max(0, Math.min(max, ply));
  state.cursor = (!isReplay() && next === max) ? null : next;
  render();
}

function stepAuto() {
  if (cursor() >= plyCount()) return stopAuto();
  goto(cursor() + 1);
}

function stopAuto() {
  clearInterval(state.autoplay);
  state.autoplay = null;
  renderStatus();
}

function toggleAuto() {
  if (state.autoplay) return stopAuto();
  if (cursor() >= plyCount()) goto(0);
  state.autoplay = setInterval(stepAuto, 700);
  renderStatus();
}

/* --- live feed ---------------------------------------------------------- */

function connect() {
  const source = new EventSource("/api/events");
  const conn = $("#conn");

  source.onopen = () => { conn.dataset.state = "live"; conn.textContent = "live"; };
  source.onerror = () => { conn.dataset.state = "lost"; conn.textContent = "reconnecting"; };

  source.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    const live = state.live;

    switch (ev.type) {
      case "snapshot": {
        const { type, ...snap } = ev;
        state.live = { ...emptyGame(), ...snap };
        break;
      }
      case "start":
        state.live = { ...emptyGame(), status: "running", white: ev.white, black: ev.black };
        state.veilOff = false;
        state.cursor = null;
        break;
      case "thinking":
        live.thinking = { color: ev.color, engine: ev.engine, since: ev.at };
        break;
      case "move":
        live.moves.push(ev.move);
        live.thinking = null;
        if (!isReplay() && state.cursor === null) {
          state.anim = { from: ev.move.from, to: ev.move.to };
        }
        break;
      case "gameover":
        live.status = "finished";
        live.result = ev.result;
        live.termination = ev.termination;
        live.pgn = ev.pgn;
        live.saved = ev.saved;
        live.thinking = null;
        loadArchive();
        break;
      case "log":
      case "error":
        live.log.push(ev);
        if (ev.type === "error") live.status = "error";
        renderLog();
        break;
      default:
        break;
    }

    if (ev.type === "log") return;           // already handled, no repaint needed
    if (isReplay() && ev.type !== "snapshot") { renderStatus(); return; }
    render();
    if (ev.type === "snapshot" || ev.type === "error") renderLog();
  };
}

/* --- controls ----------------------------------------------------------- */

async function loadConfig() {
  const cfg = await (await fetch("/api/config")).json();
  state.config = cfg;
  const d = cfg.defaults;

  for (const seat of ["white", "black"]) {
    const select = $(`#${seat}`);
    select.replaceChildren(...cfg.engines.map((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = { claude: "Claude", gemini: "Gemini", mock: "Mock (random)" }[name] || name;
      return opt;
    }));
    select.value = d[seat];
  }
  $("#claude-model").value = d.claude_model;
  $("#gemini-model").value = d.gemini_model;
  $("#delay").value = d.delay;
  $("#delay-out").value = `${Number(d.delay).toFixed(1)}s`;
  $("#max-plies").value = d.max_plies;
  $("#forfeit").checked = d.forfeit_on_illegal;
}

async function startGame() {
  const body = {
    white: $("#white").value,
    black: $("#black").value,
    claude_model: $("#claude-model").value.trim(),
    gemini_model: $("#gemini-model").value.trim(),
    delay: Number($("#delay").value),
    max_plies: Number($("#max-plies").value),
    forfeit_on_illegal: $("#forfeit").checked,
  };
  hint("");
  const res = await fetch("/api/game", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    hint((await res.json()).error || "Could not start the game.", true);
    return;
  }
  state.replay = null;
  state.cursor = null;
  stopAuto();
}

function hint(text, bad = false) {
  const el = $("#hint");
  el.textContent = text;
  el.classList.toggle("bad", bad);
}

/* --- archive ------------------------------------------------------------ */

async function loadArchive() {
  const box = $("#archive");
  let games = [];
  try {
    games = (await (await fetch("/api/games")).json()).games;
  } catch {
    box.replaceChildren(el("p", "empty", "Could not read the games folder."));
    return;
  }
  if (!games.length) {
    box.replaceChildren(el("p", "empty", "Finished games are saved here."));
    return;
  }
  box.replaceChildren(...games.map((g) => {
    const row = el("button", "game-row");
    row.append(el("span", "names", `${g.white} vs ${g.black}`));
    row.append(el("span", "score", g.result || "*"));
    row.append(el("span", "sub",
      `${(g.started || "").replace("T", " ")} · ${Math.ceil(g.plies / 2)} moves · ${g.termination || ""}`));
    row.onclick = () => openReplay(g.name);
    return row;
  }));
}

async function openReplay(name) {
  const res = await fetch(`/api/games/${encodeURIComponent(name)}`);
  if (!res.ok) return hint("Could not load that game.", true);
  const record = await res.json();
  state.replay = {
    status: "replay",
    white: record.white,
    black: record.black,
    moves: record.moves,
    log: [],
    thinking: null,
    result: record.result,
    termination: record.termination,
    pgn: record.pgn,
  };
  state.cursor = 0;
  state.veilOff = false;
  stopAuto();
  showTab("moves");
  render();
}

function backToLive() {
  state.replay = null;
  state.cursor = null;
  stopAuto();
  render();
}

/* --- tabs, theme, wiring ------------------------------------------------ */

function showTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("is-on", t.dataset.tab === name));
  $$(".tab-body").forEach((b) => { b.hidden = b.dataset.panel !== name; });
  if (name === "archive") loadArchive();
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("arena-theme", theme); } catch { /* private mode */ }
}

function wire() {
  $("#start").onclick = startGame;
  $("#stop").onclick = () => fetch("/api/stop", { method: "POST" });
  $("#flip").onclick = () => { state.flipped = !state.flipped; render(); };
  $("#theme").onclick = () => applyTheme(
    document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  $("#back-to-live").onclick = backToLive;
  // Click the result card away to study the final position.
  $("#veil").onclick = () => { state.veilOff = true; renderStatus(); };

  $("#delay").oninput = (e) => { $("#delay-out").value = `${Number(e.target.value).toFixed(1)}s`; };

  $$(".tab").forEach((tab) => { tab.onclick = () => showTab(tab.dataset.tab); });

  $$("[data-nav]").forEach((btn) => {
    btn.onclick = () => {
      const what = btn.dataset.nav;
      if (what === "play") return toggleAuto();
      stopAuto();
      if (what === "start") goto(0);
      if (what === "prev") goto(cursor() - 1);
      if (what === "next") goto(cursor() + 1);
      if (what === "end") goto(plyCount());
    };
  });

  $("#copy-pgn").onclick = () => copy(view().pgn || "", "PGN copied.");
  $("#copy-fen").onclick = () => copy(fenAt(view(), cursor()), "FEN copied.");

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea, button")) return;
    if (e.key === "ArrowLeft") { stopAuto(); goto(cursor() - 1); }
    if (e.key === "ArrowRight") { stopAuto(); goto(cursor() + 1); }
    if (e.key === "Home") { stopAuto(); goto(0); }
    if (e.key === "End") { stopAuto(); goto(plyCount()); }
    if (e.key.toLowerCase() === "f") { state.flipped = !state.flipped; render(); }
    if (e.key === " ") { e.preventDefault(); toggleAuto(); }
  });

  window.addEventListener("resize", () => renderBoard());
}

async function main() {
  try { applyTheme(localStorage.getItem("arena-theme") || "dark"); } catch { /* ignore */ }
  wire();
  render();
  await loadConfig().catch(() => hint("Could not reach the server.", true));
  connect();
  loadArchive();
  // Keeps the thinking clock ticking between events.
  setInterval(() => { if (!isReplay() && view().thinking) renderCards(); }, 100);
}

function copy(text, done) {
  navigator.clipboard.writeText(text).then(() => hint(done), () => hint("Copy failed.", true));
}

main();
