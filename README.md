# Local Council

A local-only app where a council of AI personas discuss a pitch — an idea,
a decision, something you're chewing on — and produce a written synthesis.

Five personas ship by default: **Strategist**, **Skeptic**, **Empath**,
**Future Self**, **Operator**. Each speaks in a round-robin (round 1),
then again with the full round 1 transcript in front of them (round 2),
then a "council secretary" wraps it up in a few sentences plus a one-line
summary for later recall.

Single-user. Runs entirely on your laptop. Uses
[Ollama](https://ollama.com) for inference (`qwen3.6:latest`).

## Branches: `local` vs `main`

This is the **`local`** branch — the original, fully offline version. It talks
to Ollama on your laptop, so your pitches never leave the machine and there's
no API bill; the trade-off is that it needs Ollama (and the GPU to run it).

The **`main`** branch is the deployable cousin: it swaps Ollama for the
**Anthropic API** so it can run on [Railway](https://railway.app) with no GPU,
and adds the pieces a hosted, multi-session service needs.

| | `local` (this branch) | `main` |
|---|---|---|
| Inference | Ollama on your laptop (`qwen3.6:latest`) | Anthropic API (`claude-sonnet-4-6`) |
| Where it runs | Your machine only, offline | Local **or** deployed to Railway |
| Your data | Never leaves the laptop | Pitches sent to Anthropic; sessions stored server-side |
| Auth | None | HTTP Basic Auth (enabled by env vars in prod) |
| Persistence | None | SQLite session archive (`/sessions`) |
| Memory recall | None | Relevant past sessions injected into each new pitch |
| Model picker | Single model | `/models` allowlist: Balanced · Deep · Fast |
| Secrets needed | None | `ANTHROPIC_API_KEY` (+ `COUNCIL_USER` / `COUNCIL_PASSWORD` in prod) |

**Use `local`** for private, offline, no-cost deliberation on your own hardware.
**Use `main`** when you want it hosted and reachable from anywhere.

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

Pull the model:

```bash
ollama pull qwen3.6:latest
```

Install dependencies:

```bash
uv sync
```

## Run

```bash
uv run python -m uvicorn main:app --reload
```

Open <http://localhost:8000>, type a pitch, click **Convene**. Tokens stream
live, agent-by-agent. When both rounds finish, click **Wrap** for a
synthesis.

## Customizing the council

Personas live in `personas.json`. To add a sixth persona, append to the
array; to drop one, remove the entry; restart the server. The code
iterates `len(personas)` — never hardcodes the count.

```json
[
  {
    "name": "Strategist",
    "system": "You are the Strategist on a council of advisors..."
  },
  ...
]
```

Personas are validated at startup: each must have a non-empty `name` and
`system`, and names must be unique.

## Project structure

```
.
├── main.py            # FastAPI app — backend
├── personas.json      # Persona definitions
├── templates/
│   └── index.html     # Single-page UI
├── pyproject.toml
├── README.md
└── CLAUDE.md          # Project notes
```

## Status

- [x] Two-round discussion with dynamic council size
- [x] Live SSE streaming, per-agent blocks
- [x] Synthesis + one-line summary on **Wrap**
- [x] SQLite persistence + browsable session archive
- [x] Memory recall — relevant prior sessions injected into each persona

## Stack

Python 3.13 · uv · FastAPI · sse-starlette · httpx · vanilla JS · Ollama.
