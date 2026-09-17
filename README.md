# AI-Chess

Claude and Gemini play a full game of chess against each other. You type `go` and watch.

A single Python script acts as referee: it owns the board, hands each model the current
position and the exhaustive list of legal moves, and records every move for replay. The
models never track state themselves — they get a fresh prompt every turn.

**This poses no threat whatsoever to grandmaster players.** Or club players. The opening
is usually reasonable, then things get sharp and it all gets a bit experimental. That's
half the fun.

---

## What it does

- **Claude vs Gemini**, White and Black, one move at a time with a configurable delay
- **The orchestrator owns the board** via [`python-chess`](https://python-chess.readthedocs.io/) — models are move generators, nothing more
- **Legal moves are supplied** in every prompt, so illegal output is a model failure, not a missing hint
- **Illegal moves are retried** up to 3 times with the error fed back, then a random legal move (or a forfeit, your choice)
- **Tolerant parsing** — accepts UCI, UCI buried in a sentence, or SAN, rather than burning a retry on formatting
- **Every game is saved twice**: a `.json` with full move-by-move detail and a standard `.pgn` for any chess GUI
- **Replay mode** steps back through a saved game move by move
- **Mock mode** runs the entire loop with random-move engines — no API keys, no cost

---

## Requirements

- Python 3.10+
- An Anthropic API key
- A Google Gemini API key

Built and tested on WSL (Ubuntu). Works anywhere Python does.

---

## Setup

```bash
git clone https://github.com/Tony-A-Michailidis/AI-Chess
cd AI-Chess
python3 -m venv .venv
source .venv/bin/activate
pip install chess anthropic google-genai
```

On Windows without WSL, activate with `.venv\Scripts\activate` instead.

If `venv` fails with an `ensurepip` error on Debian/Ubuntu:

```bash
sudo apt update && sudo apt install python3-venv
```

---

## Getting the API keys

Two keys, from two providers. Both are pay-as-you-go and **separate from any chat
subscription** — a Claude Pro/Max plan or Gemini Advanced does not include API credit.

### Anthropic

1. Sign in at [console.anthropic.com](https://console.anthropic.com)
2. Load credit — there's no free tier, and the API errors out on a zero balance. $5 covers a lot of chess.
3. **API keys** → **Create Key**, name it, and copy it immediately. It is shown once.
4. Optional but recommended: **Limits** → set a monthly spend cap before you point a loop at a paid API.

### Google

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. **Create API key**
3. There's a free tier with rate limits that will cover a game or two.

### Setting them

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=AIza...
```

That lasts for the current shell only. For something persistent, use a `.env` file:

```bash
pip install python-dotenv
```

```
# .env
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...
```

Then add `from dotenv import load_dotenv; load_dotenv()` near the top of
`chess_arena.py`, above the engine classes.

**Never commit your keys.** `.env` is already in `.gitignore`.

---

## Running it

Free dry run first — no API calls, no keys needed:

```bash
python chess_arena.py --mock
```

Two random-move engines play a full game so you can see the loop, the retry path, and
the logging work. Then the real thing:

```bash
python chess_arena.py
```

Type `go` and press enter. Ctrl-C to bail out at any point.

When it finishes it prints the paths it saved to. Replay with:

```bash
python chess_arena.py --replay games/game-20260917-143000.json
```

---

## Options

| Flag | What it does |
|---|---|
| `--mock` | Random-move engines, no API calls, no keys |
| `--replay PATH` | Step through a saved game `.json` |
| `--delay N` | Seconds between moves (default `2.0`) |
| `--swap` | Gemini plays White (default: Claude plays White) |
| `--ascii` | Plain letters instead of Unicode pieces |
| `--forfeit-on-illegal` | Lose the game instead of falling back to a random legal move |
| `--max-plies N` | Hard stop so a shuffling draw can't run forever (default `300`) |
| `--no-prompt` | Start immediately instead of waiting for `go` |

---

## Configuration

Model IDs are constants at the top of `chess_arena.py`:

```python
CLAUDE_MODEL = "claude-sonnet-5"   # "claude-opus-5" plays better and costs more
GEMINI_MODEL = "gemini-3.8-flash"
```

These move fast. If either 404s, check the
[Anthropic models page](https://docs.claude.com/en/docs/about-claude/models) or the
[Gemini models page](https://ai.google.dev/gemini-api/docs/models).

Also adjustable up top: `MAX_RETRIES`, `MOVE_DELAY`, `MAX_PLIES`, `GAMES_DIR`, and the
system prompt.

---

## What gets saved

Each game writes two files into `games/`.

**`game-TIMESTAMP.json`** — the full record:

```json
{
  "ply": 7,
  "color": "White",
  "engine": "Claude",
  "uci": "g1f3",
  "san": "Nf3",
  "fen_before": "...",
  "fen_after": "...",
  "seconds": 1.83,
  "attempts": [
    {"raw": "I'll develop the knight", "accepted": false},
    {"raw": "g1f3", "accepted": true}
  ],
  "fallback": false
}
```

The `attempts` array is the interesting part — it preserves the raw text of everything
the model got wrong before it landed on something legal.

**`game-TIMESTAMP.pgn`** — standard PGN, opens in Lichess, SCID, ChessBase, anything.

---

## Notes and gotchas

- **Unicode pieces rendering as boxes?** Your terminal font lacks the glyphs. Use `--ascii`.
- **`ModuleNotFoundError` on a new terminal?** Run `source .venv/bin/activate` first.
- **Cost:** each move is a few hundred input tokens and a handful of output tokens. A full game on Sonnet runs well under a dollar. Opus is several times that, and reasoning-heavy Gemini configs can surprise you — watch the first game before queuing a tournament.
- **Gemini returning empty responses?** Reasoning tokens count against `max_output_tokens`. It's set generously, but you can raise it in `GeminiEngine.ask()`.
- **Random fallbacks distort results.** If you're actually comparing the two models, use `--forfeit-on-illegal` for a clean scoreline.

---

## Ideas / contributions welcome

- A third model, and a round-robin tournament runner
- Stockfish as a benchmark opponent, or for per-move centipawn evaluation
- Let the models write a one-line comment per move and log it alongside
- Elo tracking across many games
- A web UI for the replays

PRs and issues welcome.

---

## License

MIT — see [LICENSE](LICENSE).
