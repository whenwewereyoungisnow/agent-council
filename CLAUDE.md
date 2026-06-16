# CLAUDE.md — Local Council

> Drop this into the project root as `CLAUDE.md`. It supplements the global `CLAUDE.md`.

## Project
A council of AI agents that discuss pitches (ideas, decisions, feelings).
Single-user (one Basic Auth login). Runs locally during development; deploys
to Railway as a public-but-auth'd service. Persists sessions to SQLite.
Recalls relevant prior sessions when a new pitch comes in.

## Stack notes
Inherits from global `CLAUDE.md`. One deviation: this project uses the
Anthropic API directly via `httpx`, not Ollama (so it can run on Railway
where there's no GPU and the filesystem is ephemeral).

- Single FastAPI app: `main.py`.
- Single HTML page: `templates/index.html`.
- Personas defined in `personas.json` — hand-edited.
- SQLite db: `council.db`, sqlite3 stdlib, no ORM.
- All LLM calls via `httpx`, async, against `https://api.anthropic.com/v1/messages`.
- SSE via `sse-starlette`.

## Model
Default `claude-sonnet-4-6` (override via `ANTHROPIC_MODEL` env var).

- `max_tokens: 1024` for both agent turns and the wrap synthesis — two
  paragraphs of council output, three to five sentences of synthesis,
  fits comfortably.
- System prompt is a top-level field on Anthropic's API, **not** a message
  with `role: system`. Mixing those up is a silent failure mode.
- Streaming: enabled for agent turns. Non-streaming for the wrap synthesis.
- Cold start is just network latency (~300ms-1s), not model load.
- **Model picker.** `/pitch` and `/wrap` take an optional `model`, validated
  against a server-side allowlist `MODEL_CHOICES`: `claude-sonnet-4-6` (Balanced),
  `claude-opus-4-8` (Deep), `claude-haiku-4-5` (Fast). Omitted → `DEFAULT_MODEL`
  (the `ANTHROPIC_MODEL` env default). The chosen model governs both the agent
  turns and the wrap synthesis; the frontend reads the picker once at convene
  time and reuses it for the wrap. `GET /models` is the allowlist's single source
  of truth for the UI. Never forward a raw client model string to the API.
- **Memory recall always uses Haiku** (`RECALL_MODEL = claude-haiku-4-5`),
  independent of the discussion model — relevance-ranking is a Haiku-class task.

## Auth
HTTP Basic Auth, applied as a global FastAPI dependency. Gated by env vars:

- `COUNCIL_USER` and `COUNCIL_PASSWORD` — both unset → auth disabled (local dev).
- Both set → all routes require Basic Auth. Browser caches credentials per origin.

## Required environment variables

| Var | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Calls to `/v1/messages` |
| `ANTHROPIC_MODEL` | no | Override default `claude-sonnet-4-6` |
| `COUNCIL_USER` | prod only | Basic Auth username |
| `COUNCIL_PASSWORD` | prod only | Basic Auth password |
| `PORT` | Railway-injected | Bind port (Railway sets this) |

## API endpoints

```
POST /pitch
  body:    { "pitch": "...", "model": "..." }   ("model" optional; allowlisted)
  returns: SSE stream
    events:
      - { event: "memory_loaded",     data: { session_ids: [int, ...] } }   (Phase 5)
      - { event: "agent_speaking",    data: { agent: "Strategist", round: 1 } }
      - { event: "token",             data: { agent: "Strategist", round: 1, token: "..." } }
      - { event: "agent_done",        data: { agent: "Strategist", round: 1 } }
      ... repeats for each agent × each round
      - { event: "discussion_complete", data: { transcript_id: "..." } }
      - { event: "error",             data: { message: "..." } }   (on failure)

POST /wrap
  body:    { "transcript": [...], "pitch": "...", "model": "..." }   ("model" optional; allowlisted)
  returns: { "synthesis": "...", "summary": "...", "session_id": int }   (Phase 5: + session_id)
  side:    persists session row to SQLite (Phase 5)

GET  /models
  returns: { models: [{ id, label }, ...], default: "<model id>" }   (picker source of truth)

GET  /sessions                                                            (Phase 5)
  returns: [{ id, pitch, summary, created_at }, ...]   (most-recent first)

GET  /sessions/{id}                                                       (Phase 5)
  returns: full session including transcript_json + synthesis

GET  /
  returns: index.html
```

## SQLite schema (Phase 5)

```sql
CREATE TABLE IF NOT EXISTS sessions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  pitch           TEXT NOT NULL,
  transcript_json TEXT NOT NULL,        -- JSON array: [{agent, round, content}, ...]
  synthesis       TEXT,
  summary         TEXT,                 -- LLM-generated, 1 sentence, used for recall
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions (created_at DESC);
```

Personas live in `personas.json`, not in the db, for v1 — easier to edit.

On Railway, `council.db` needs a Volume mount or a switch to Railway Postgres
when Phase 5 lands. The container filesystem is ephemeral on each deploy.

## File layout

```
agent-council/
├── main.py             # FastAPI app, all backend logic
├── personas.json       # Persona definitions — edit to add/remove/change
├── templates/
│   └── index.html      # Single page UI
├── council.db          # SQLite, gitignored (Phase 5)
├── pyproject.toml
├── Procfile            # Railway start command
└── .gitignore
```

## Personas

Hybrid: 5 ship as defaults, but `personas.json` defines the full council.
Append to add a 6th persona, remove an entry to drop one, restart the
server to pick up changes. Code reads the file once at startup, validates
each entry, and iterates by length — never hardcode the count.

## Conventions specific to this project

- **Council size is dynamic.** Code reads `personas.json` once at startup
  and iterates over whatever's there. Never hardcode the count.
- **One async `httpx.AsyncClient` per `/pitch` request**, reused across the
  full round-robin (open in `/pitch`, close at the end). `/wrap` opens its
  own short-lived client.
- **Agent calls** use Anthropic's `/v1/messages` with `stream: true`.
  **Synthesis** uses the same endpoint with `stream: false`.
- **Whitespace from streamed text deltas is preserved verbatim** when
  forwarding to SSE — Anthropic's `content_block_delta` events carry their
  own spaces; don't re-add or strip.
- **Transcripts** are stored as JSON arrays of `{agent, round, content}`
  objects, not concatenated strings — keeps round/agent attribution intact
  for replay and persistence.
- **Agent context construction (round 1):** system prompt = persona + memory
  dossier (if any). User message = the pitch, followed (if there are prior
  contributions in this round) by an inlined block of those prior
  contributions, followed by an explicit instruction to respond as this
  agent.
- **Agent context construction (round 2):** same approach as round 1.
  System prompt = persona + "You have heard the others speak. Engage with
  specific points they made. Do not just restate your view." User message =
  the pitch, followed by the full round 1 transcript inlined, followed by
  the round-2 instruction to respond as this agent.
- **User message format (both rounds):**
  ```
  PITCH:
  <pitch text>

  <if prior contributions exist:>
  PRIOR COUNCIL CONTRIBUTIONS:
  [Strategist, round 1]: <content>
  [Skeptic, round 1]: <content>
  ...

  Now respond as the <ThisAgentName>. Speak in your own voice. Two paragraphs maximum.
  ```
- **Memory dossier format** when injected into system prompts:
  `"Earlier council discussions you remember. Reference them naturally by what was discussed, not by number or date:\n- (date) <summary>\n- (date) <summary>"`.
  Empty if nothing relevant. The persona-facing dossier omits the session id so
  the council references discussions by substance, not "Session N".
- **Relevance check prompt** (Phase 5) asks for a JSON array of integer IDs
  only. Parse robustly — empty array on parse failure (don't crash the
  pitch flow over a bad JSON response).
- **Errors during streaming** are caught, emitted as an `error` SSE event,
  and the stream closes cleanly. The frontend handles this without freezing.

## Local development

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run python -m uvicorn main:app --reload
```

`COUNCIL_USER` / `COUNCIL_PASSWORD` left unset → auth bypass for local dev.

## Deploying to Railway

1. Push to GitHub (this repo is already wired).
2. Railway detects `pyproject.toml` + `.python-version` (3.13) via Nixpacks.
3. `Procfile` provides the start command (`uvicorn main:app --host 0.0.0.0 --port $PORT`).
4. Set env vars in Railway → Variables: `ANTHROPIC_API_KEY`, `COUNCIL_USER`, `COUNCIL_PASSWORD`, optionally `ANTHROPIC_MODEL`.
5. Railway builds and exposes a public URL. Browser will prompt for Basic Auth on first hit.

## Things to verify before "done"

- Watch tokens stream in the browser, not just over `curl`.
- Confirm round 2 references round 1 specifically (read the transcript,
  don't just check it ran).
- Run a memory-recall test (Phase 5): two related pitches in sequence, and
  confirm session 2 visibly references session 1.
- Run an unrelated pitch and confirm recall returns nothing (no false positives).
