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
- [ ] SQLite persistence
- [ ] Memory recall — relevant prior sessions injected into each persona

## Stack

Python 3.13 · uv · FastAPI · sse-starlette · httpx · vanilla JS · Ollama.
