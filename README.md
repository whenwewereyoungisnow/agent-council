# Local Council

A council of AI personas that discuss a pitch — an idea, a decision,
something you're chewing on — and produce a written synthesis.

Five personas ship by default: **Strategist**, **Skeptic**, **Empath**,
**Future Self**, **Operator**. Each speaks in a round-robin (round 1),
then again with the full round 1 transcript in front of them (round 2),
then a "council secretary" wraps it up in a few sentences plus a one-line
summary for later recall.

Single-user (one Basic Auth login). Runs locally for development; deploys
to Railway for access from anywhere. Uses
[Anthropic's Claude API](https://docs.anthropic.com/) for inference
(`claude-sonnet-4-6` by default).

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

Install dependencies:

```bash
uv sync
```

Set your Claude API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run locally

```bash
uv run python -m uvicorn main:app --reload
```

Open <http://localhost:8000>, type a pitch, click **Convene the council**.
Tokens stream live, agent-by-agent. When both rounds finish, click
**Hear the council's synthesis** for a wrap.

For local dev, `COUNCIL_USER` and `COUNCIL_PASSWORD` are left unset, so
Basic Auth is disabled.

## Deploy to Railway

The repo is Railway-ready. Steps:

1. Push to GitHub (this repo is already wired).
2. In Railway, **New Project → Deploy from GitHub** and pick the repo.
3. Railway detects `pyproject.toml` + `.python-version` and builds via Nixpacks.
4. The included `Procfile` provides the start command.
5. In **Variables**, set:
   - `ANTHROPIC_API_KEY` — your Anthropic API key (required).
   - `COUNCIL_USER` and `COUNCIL_PASSWORD` — credentials for Basic Auth.
     Without these, the public URL is unauthenticated.
   - `ANTHROPIC_MODEL` — optional override (default `claude-sonnet-4-6`).
6. Railway exposes a public URL. Browser prompts for the Basic Auth on
   first hit and caches the credentials.

Cost ballpark on Sonnet 4.6: ~$0.13 per full discussion (10 streamed agent
turns plus the wrap). Haiku 4.5 is ~$0.05; Opus 4.8 is ~$0.67.

The pitch screen has a per-discussion **Deliberation** picker — **Balanced**
(`claude-sonnet-4-6`), **Deep** (`claude-opus-4-8`), **Fast** (`claude-haiku-4-5`)
— validated against a server-side allowlist. Memory recall always runs on Haiku
regardless of the pick, since relevance-ranking doesn't need the council's voice.

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
├── Procfile           # Railway start command
├── README.md
└── CLAUDE.md          # Project notes
```

## Status

- [x] Two-round discussion with dynamic council size
- [x] Live SSE streaming, per-agent blocks
- [x] Synthesis + one-line summary on **Wrap**
- [x] Anthropic Claude API backend; Railway-ready with Basic Auth
- [x] SQLite persistence + session archive (on Railway, mount a Volume and point `COUNCIL_DB_PATH` at it)
- [x] Memory recall — relevant prior sessions injected into each persona

## Stack

Python 3.13 · uv · FastAPI · sse-starlette · httpx · vanilla JS · Anthropic Claude API.
