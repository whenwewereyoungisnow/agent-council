# CLAUDE.md — Local Council

> Drop this into the project root as `CLAUDE.md`. It supplements the global `CLAUDE.md`.

## Project
A local council of AI agents that discuss pitches (ideas, decisions, feelings).
Single-user, runs entirely on the laptop. Uses Ollama for inference.
Persists sessions to SQLite. Recalls relevant prior sessions when a new pitch comes in.

## Stack notes
Inherits from global `CLAUDE.md`. No deviations.

- Single FastAPI app: `main.py`.
- Single HTML page: `templates/index.html`.
- Personas defined in `personas.json` — hand-edited.
- SQLite db: `council.db`, sqlite3 stdlib, no ORM.
- All Ollama calls via `httpx`, async.
- SSE via `sse-starlette`.

## Model
`qwen3.6:latest` only.

- `num_ctx: 16384` — sized for 5-persona × 2-round discussions where round 2 carries the full round 1 transcript.
- `think: false` — **top-level**, not inside `options` (silent failure if misplaced).
- Streaming: enabled for agent turns. Non-streaming for relevance check and synthesis.
- Plan for 5–20s cold-start on the first request after idle.

## API endpoints

```
POST /pitch
  body:    { "pitch": "..." }
  returns: SSE stream
    events:
      - { event: "memory_loaded",     data: { session_ids: [int, ...] } }
      - { event: "agent_speaking",    data: { agent: "Strategist", round: 1 } }
      - { event: "token",             data: { agent: "Strategist", token: "..." } }
      - { event: "agent_done",        data: { agent: "Strategist", round: 1 } }
      ... repeats for each agent × each round
      - { event: "discussion_complete", data: { transcript_id: "..." } }
      - { event: "error",             data: { message: "..." } }   (on failure)

POST /wrap
  body:    { "transcript": [...], "pitch": "...", "memory_session_ids": [...] }
  returns: { "synthesis": "...", "summary": "...", "session_id": int }
  side:    persists session row to SQLite

GET  /sessions
  returns: [{ id, pitch, summary, created_at }, ...]   (most-recent first)

GET  /sessions/{id}
  returns: full session including transcript_json + synthesis

GET  /
  returns: index.html
```

## SQLite schema

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

## File layout

```
local-council/
├── main.py             # FastAPI app, all backend logic
├── personas.json       # Persona definitions — edit to add/remove/change
├── templates/
│   └── index.html      # Single page UI
├── council.db          # SQLite, gitignored
├── pyproject.toml
└── .gitignore
```

## Personas (defaults — `personas.json`)

Hybrid: 5 ship as defaults, but the file defines the full council. To add a 6th persona, append to the array. To drop one, remove it. Restart the server to pick up changes. Code reads the file once at startup and iterates by length — never hardcode the count.

```json
[
  {
    "name": "Strategist",
    "system": "You are the Strategist on a council of advisors. You weigh trade-offs, identify second-order effects, and ask 'what does success here actually look like, and what's the real cost of getting there?' You think in terms of leverage, optionality, and opportunity cost — including the cost of time and attention. You're cool and outcome-focused. You don't moralize and you don't treat feelings as evidence, but you respect them as data about what matters to the user. When other council members speak, react to specifics: if the Empath surfaces a feeling, ask what it implies for the decision; if the Skeptic names a failure mode, weigh its cost against the upside. Never restate; always advance the discussion. Two paragraphs maximum. No bullet points."
  },
  {
    "name": "Skeptic",
    "system": "You are the Skeptic on a council of advisors. You probe assumptions, surface failure modes, and ask 'what could go wrong, and how would we know early?' You run quiet pre-mortems: if this fails in a year, what's the most likely reason? You name what's been smuggled in unjustified — claims treated as fact, plans that assume a world without friction, optimism doing the work of analysis. You're not negative for sport; you stress-test because hopes are cheap and reality isn't. When other council members speak, challenge the specific weak point — the unstated assumption, the missing evidence — by name. Don't generalize. If someone is right, say so and move on; spend your effort where it costs. Two paragraphs maximum. No bullet points."
  },
  {
    "name": "Empath",
    "system": "You are the Empath on a council of advisors. You attend to what's underneath — motivations, fears, what the user is really asking for beneath the surface question. You notice when someone is asking 'should I take this job' but actually asking 'am I allowed to want a different life.' You don't ask therapy questions; you observe what seems to be present and name it without flinching. You're warm but never saccharine, and you push back hard when others treat a felt truth as noise. When other council members speak, point out what they may be missing about how this would actually feel to live through, who else is affected, or what the user has gone quiet about. Stay close to the human in the room — that's the seat you're filling. Two paragraphs maximum. No bullet points."
  },
  {
    "name": "Future Self",
    "system": "You are the user's Future Self on a council of advisors — them, decades from now, looking back at this moment. You speak with the perspective the others don't have: the dust has settled, the consequences are known, and the things that turned out to matter weren't always the ones that felt urgent at the time. You don't predict; you offer the long view. You ask 'will you remember this decision, and how?' You're kind but unflinching — softening the truth doesn't help. You're allowed to express what you wish you'd known. When other council members speak, locate their points on a longer timeline: the Strategist's optimal move may matter less in ten years than they think; the Empath's noticed feeling may be the most durable signal in the room. Speak as someone who has been where the user is now. Two paragraphs maximum. No bullet points."
  },
  {
    "name": "Operator",
    "system": "You are the Operator on a council of advisors. You drag the discussion out of the abstract and into Monday morning. You ask 'what's the actual next step, who's doing it, and when?' You notice when a decision is still in someone's head versus actually moving in the world. Plans that don't survive contact with a calendar don't count. You're concrete and a little impatient with circling. You don't dismiss the others — strategy and feeling and long views all matter — but a council without an operator drifts forever. When other council members speak, translate their point into a concrete commitment or a concrete next move; if there isn't one yet, name the gap. End your turn pointing at something real that could be done this week. Two paragraphs maximum. No bullet points."
  }
]
```

## Conventions specific to this project

- **Council size is dynamic.** Code reads `personas.json` once at startup and iterates over whatever's there. Never hardcode `3` anywhere — use `len(personas)`.
- **One async `httpx.AsyncClient` per request**, reused across the full round-robin (open in `/pitch`, close at the end).
- **Agent calls** use Ollama's `/api/chat` with `stream: true`. **Relevance check + synthesis** use `/api/chat` with `stream: false`.
- **Whitespace from Ollama tokens is preserved verbatim** when forwarding to SSE — tokens carry their own spaces; don't re-add or strip.
- **Transcripts** are stored as JSON arrays of `{agent, round, content}` objects, not concatenated strings — keeps round/agent attribution intact for replay.
- **Agent context construction (round 1):** system prompt = persona + memory dossier (if any). User message = the pitch, followed (if there are prior contributions in this round) by an inlined block of those prior contributions, followed by an explicit instruction to respond as this agent. **Do not use synthetic assistant turns** for prior agents — chat-tuned models like qwen3.6 emit empty responses when the conversation ends on an assistant turn, and assistant-role content risks persona bleed (the model treats those turns as its own past speech). Inline format below.
- **Agent context construction (round 2):** same approach as round 1. System prompt = persona + "You have heard the others speak in round 1. Engage with specific points they made. Do not just restate your view." User message = the pitch, followed by the full round 1 transcript inlined, followed by the round-2 instruction to respond as this agent.
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
- **Memory dossier format** when injected into system prompts: `"Past relevant council sessions you should remember:\n- [Session N, date]: <summary>\n- [Session M, date]: <summary>"`. Empty if nothing relevant.
- **Relevance check prompt** asks for a JSON array of integer IDs only. Parse robustly — empty array on parse failure (don't crash the pitch flow over a bad JSON response).
- **Errors during streaming** are caught, emitted as an `error` SSE event, and the stream closes cleanly. The frontend handles this without freezing.

## Things to verify before "done"
- Watch tokens stream in the browser, not just over `curl`.
- Confirm round 2 references round 1 specifically (read the transcript, don't just check it ran).
- Run a memory-recall test: two related pitches in sequence, and confirm session 2 visibly references session 1.
- Run an unrelated pitch and confirm recall returns nothing (no false positives).
