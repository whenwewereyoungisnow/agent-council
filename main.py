import asyncio
import json
import os
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

# ─────────── Anthropic config ───────────
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise RuntimeError(
        "ANTHROPIC_API_KEY environment variable is required. "
        "Set it locally with `export ANTHROPIC_API_KEY=sk-ant-...` "
        "or in the Railway service variables."
    )

# Models the picker offers, in display order. Keys are the API model IDs we will
# actually forward; values are the labels the UI shows. A client-supplied model
# is validated against these keys (we never forward an arbitrary string), so this
# dict is the single source of truth for both the API and the frontend's picker.
MODEL_CHOICES = {
    "claude-sonnet-4-6": "Balanced",
    "claude-opus-4-8": "Deep",
    "claude-haiku-4-5": "Fast",
}
# Default when a request omits `model`. ANTHROPIC_MODEL can override it to any
# string (admin trust); the allowlist only constrains client-chosen models.
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
# Memory recall is relevance-ranking, not council voice — a Haiku-class task.
# Pin it to Haiku regardless of the discussion model: cheaper, faster first token.
RECALL_MODEL = "claude-haiku-4-5"

MAX_TOKENS_AGENT = 1024
MAX_TOKENS_WRAP = 1024
MAX_TOKENS_RECALL = 256  # relevance check returns only a short JSON array of ids

# ─────────── Auth (HTTP Basic, gated by env vars) ───────────
COUNCIL_USER = os.environ.get("COUNCIL_USER")
COUNCIL_PASSWORD = os.environ.get("COUNCIL_PASSWORD")
_basic = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:  # noqa: B008
    """No-op locally (env vars unset). Enforces Basic Auth in deployment."""
    if not COUNCIL_USER or not COUNCIL_PASSWORD:
        return
    unauthorized = HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": 'Basic realm="Council"'},
    )
    if creds is None:
        raise unauthorized
    user_ok = secrets.compare_digest(creds.username, COUNCIL_USER)
    pass_ok = secrets.compare_digest(creds.password, COUNCIL_PASSWORD)
    if not (user_ok and pass_ok):
        raise unauthorized


# ─────────── Personas ───────────
ROOT = Path(__file__).parent


def _load_personas() -> list[dict]:
    data = json.loads((ROOT / "personas.json").read_text())
    if not isinstance(data, list) or not data:
        raise RuntimeError("personas.json must be a non-empty JSON array")
    seen: set[str] = set()
    for i, p in enumerate(data):
        if not isinstance(p, dict):
            raise RuntimeError(f"personas[{i}] is not an object")
        for key in ("name", "system"):
            if not isinstance(p.get(key), str) or not p[key].strip():
                raise RuntimeError(f"personas[{i}] missing non-empty string '{key}'")
        if p["name"] in seen:
            raise RuntimeError(f"personas.json has duplicate name: {p['name']!r}")
        seen.add(p["name"])
    return data


PERSONAS: list[dict] = _load_personas()


# ─────────── SQLite persistence ───────────
# Path is env-overridable so Railway can point it at a mounted Volume; the
# default writes council.db next to main.py for local dev. The container
# filesystem is ephemeral, so without a Volume the db is wiped on each deploy.
DB_PATH = os.environ.get("COUNCIL_DB_PATH", str(ROOT / "council.db"))


def _init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              pitch           TEXT NOT NULL,
              transcript_json TEXT NOT NULL,
              synthesis       TEXT,
              summary         TEXT,
              created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_created
              ON sessions (created_at DESC);
            """
        )
    # Log the resolved path once so "archive empty after deploy" is diagnosable.
    print(f"[council] SQLite DB at {os.path.abspath(DB_PATH)}")


def _db() -> sqlite3.Connection:
    """A fresh per-request connection. Single-user scale — no pool needed."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


_init_db()

app = FastAPI(dependencies=[Depends(require_auth)])


# ─────────── Models ───────────
class PitchBody(BaseModel):
    pitch: str = Field(..., min_length=1)
    model: str | None = None


class TranscriptEntry(BaseModel):
    agent: str
    round: int
    content: str


class WrapBody(BaseModel):
    pitch: str = Field(..., min_length=1)
    transcript: list[TranscriptEntry] = Field(..., min_length=1)
    model: str | None = None


# ─────────── Formatting ───────────
def fmt_turn(agent: str, round_num: int, content: str) -> str:
    return f"[{agent}, round {round_num}]: {content}"


def format_transcript_for_wrap(transcript: list[TranscriptEntry]) -> str:
    return "\n\n".join(fmt_turn(e.agent, e.round, e.content) for e in transcript)


def parse_synthesis(raw: str) -> dict:
    """Parse the model's JSON output. Falls back to plain text if malformed."""
    candidates = [raw.strip()]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])

    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "synthesis" in obj and "summary" in obj:
            return {"synthesis": str(obj["synthesis"]), "summary": str(obj["summary"])}

    return {"synthesis": raw.strip(), "summary": ""}


# ─────────── Anthropic client ───────────
def _anthropic_headers() -> dict:
    return {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _claude_payload(
    system: str, user: str, *, model: str, stream: bool, max_tokens: int
) -> dict:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "stream": stream,
    }


class AgentStreamError(Exception):
    """A streamed agent turn failed. The message is safe to show the user."""


def _anthropic_error_message(body: object, status: int | None = None) -> str:
    """Human reason from an Anthropic error body: {error: {type, message}}."""
    err = body.get("error") if isinstance(body, dict) else None
    parts = (
        [str(err[k]) for k in ("type", "message") if err.get(k)]
        if isinstance(err, dict)
        else []
    )
    detail = ": ".join(parts) if parts else "unknown error"
    if status is not None:
        return f"Anthropic API error (HTTP {status}): {detail}"
    return f"Anthropic API error: {detail}"


def _stream_error_text(exc: Exception) -> str:
    """A user-facing reason for a failed agent turn."""
    if isinstance(exc, AgentStreamError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "The model stopped responding (timeout)."
    if isinstance(exc, httpx.HTTPError):
        return "Lost the connection to the Anthropic API."
    return f"Unexpected error: {exc}"


async def stream_claude_text(
    client: httpx.AsyncClient, system: str, user: str, model: str
) -> AsyncIterator[str]:
    """Yield text chunks from a streaming /v1/messages call.

    Raises AgentStreamError on a non-200 response (reads the body so 401/429
    carry Anthropic's real reason) or on a mid-stream `error` frame, rather than
    silently ending the stream.
    """
    payload = _claude_payload(
        system, user, model=model, stream=True, max_tokens=MAX_TOKENS_AGENT
    )
    async with client.stream(
        "POST", ANTHROPIC_URL, headers=_anthropic_headers(), json=payload
    ) as resp:
        if resp.status_code != 200:
            # A streamed response leaves the body unread; pull it explicitly so
            # the error carries Anthropic's message, not just a bare status.
            raw = await resp.aread()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = None
            raise AgentStreamError(_anthropic_error_message(body, resp.status_code))
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            ctype = chunk.get("type")
            if ctype == "content_block_delta":
                delta = chunk.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield text
            elif ctype == "error":
                # Anthropic can emit an error frame after a 200 (e.g. overloaded
                # mid-generation) then stop. Surface it instead of swallowing.
                raise AgentStreamError(_anthropic_error_message(chunk))
            elif ctype == "message_stop":
                return


async def claude_chat(
    client: httpx.AsyncClient, system: str, user: str, model: str, max_tokens: int
) -> str:
    """Non-streaming /v1/messages call. Returns concatenated text content."""
    payload = _claude_payload(
        system, user, model=model, stream=False, max_tokens=max_tokens
    )
    resp = await client.post(
        ANTHROPIC_URL, headers=_anthropic_headers(), json=payload, timeout=180.0
    )
    resp.raise_for_status()
    body = resp.json()
    blocks = body.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


# ─────────── Prompt construction ───────────
ROUND2_SYSTEM_SUFFIX = (
    "\n\nYou have heard the others speak. "
    "Engage with specific points they made. "
    "Do not just restate your view."
)


def build_prompt(
    persona: dict, pitch: str, prior: list[dict], round_num: int, dossier: str = ""
) -> tuple[str, str]:
    """Returns (system, user) for the Anthropic API."""
    system = persona["system"]
    if round_num == 2:
        system += ROUND2_SYSTEM_SUFFIX
    if dossier:
        system += "\n\n" + dossier

    parts = [f"PITCH:\n{pitch}"]
    if prior:
        prior_block = "\n".join(
            fmt_turn(p["agent"], p["round"], p["content"]) for p in prior
        )
        parts.append(f"PRIOR COUNCIL CONTRIBUTIONS:\n{prior_block}")
    parts.append(
        f"Now respond as the {persona['name']}. "
        "Speak in your own voice. Two paragraphs maximum."
    )
    return system, "\n\n".join(parts)


# ─────────── Memory recall (Phase 5) ───────────
MAX_RECALL_CANDIDATES = 50  # cap on how many past summaries we weigh per pitch


def _oneline(text: str) -> str:
    """Collapse a summary to one line so the dossier/listing formatting holds."""
    return " ".join(text.split())


def _recent_summaries() -> list[dict]:
    """Most-recent sessions that carry a usable summary, for relevance ranking."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, created_at, summary FROM sessions "
            "WHERE summary IS NOT NULL AND TRIM(summary) != '' "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (MAX_RECALL_CANDIDATES,),
        ).fetchall()
    return [dict(r) for r in rows]


def parse_id_list(raw: str, valid_ids: set[int]) -> list[int]:
    """Extract a JSON array of ints from model output; [] on any failure.

    Scans each flat ``[...]`` span and uses the first that parses as a JSON
    list, so a stray bracket elsewhere in the model's prose can't swallow the
    real answer. Keeps only ids that exist, preserving order and dropping
    duplicates. Booleans are excluded (bool is an int subclass).
    """
    for match in re.finditer(r"\[[^\[\]]*\]", raw):
        try:
            arr = json.loads(match.group())
        except json.JSONDecodeError:
            continue
        if not isinstance(arr, list):
            continue
        out: list[int] = []
        for x in arr:
            if isinstance(x, bool) or not isinstance(x, int):
                continue
            if x in valid_ids and x not in out:
                out.append(x)
        return out
    return []


async def recall_relevant_sessions(
    client: httpx.AsyncClient, pitch: str
) -> tuple[list[int], str]:
    """Pick prior sessions relevant to this pitch. Returns (ids, dossier).

    Best-effort: any failure yields ([], "") so the pitch flow never breaks.
    """
    try:
        candidates = _recent_summaries()
        if not candidates:
            return [], ""

        by_id = {c["id"]: c for c in candidates}
        listing = "\n".join(
            f"[{c['id']}, {(c['created_at'] or '')[:10]}]: {_oneline(c['summary'])}"
            for c in candidates
        )
        system = (
            "You are the council's memory. Given a new pitch and a list of past "
            "council sessions (each with an id, date, and one-line summary), decide "
            "which past sessions are genuinely relevant to the new pitch — a similar "
            "topic, decision, tension, or theme. Be selective: relevance must be "
            "real, not superficial word overlap."
        )
        user = (
            f"NEW PITCH:\n{pitch}\n\n"
            f"PAST SESSIONS:\n{listing}\n\n"
            "Return ONLY a JSON array of the integer ids of the relevant past "
            "sessions, most relevant first, e.g. [12, 9]. If none are relevant, "
            "return []. No other text."
        )
        # Cap recall on the critical path so a slow API call can't freeze the
        # discussion behind the "Convening" overlay; we degrade to no memory.
        async with asyncio.timeout(20.0):
            raw = await claude_chat(
                client, system, user, RECALL_MODEL, MAX_TOKENS_RECALL
            )
        ids = parse_id_list(raw, set(by_id))
        if not ids:
            return [], ""

        dossier_lines = "\n".join(
            f"- ({(by_id[i]['created_at'] or '')[:10]}) {_oneline(by_id[i]['summary'])}"
            for i in ids
        )
        dossier = (
            "Earlier council discussions you remember. Reference them naturally "
            "by what was discussed, not by number or date:\n" + dossier_lines
        )
        return ids, dossier
    except Exception as e:
        # Recall is a nice-to-have; degrade to no memory rather than break /pitch.
        print(f"[council] recall failed, continuing without memory: {e}")
        return [], ""


# ─────────── Routes ───────────
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "templates" / "index.html")


# A 60s read timeout converts a silently hung agent stream into a surfaced error
# instead of an unbounded wait. Recall and /wrap pass their own per-request
# timeouts, so this governs only the streaming agent turns.
PITCH_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def resolve_model(requested: str | None) -> str:
    """Validate a client-chosen model against the allowlist, or fall back to the
    configured default. Raises 400 on an unknown model so an arbitrary client
    string is never forwarded to the Anthropic API."""
    if not requested:
        return DEFAULT_MODEL
    if requested not in MODEL_CHOICES:
        raise HTTPException(400, f"Unknown model: {requested!r}")
    return requested


@app.post("/pitch")
async def pitch(body: PitchBody) -> EventSourceResponse:
    # Validate before the stream opens, so a bad model is a clean 400 the
    # frontend can show rather than a mid-stream error.
    model = resolve_model(body.model)

    async def event_stream() -> AsyncIterator[dict]:
        transcript: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=PITCH_TIMEOUT) as client:
                session_ids, dossier = await recall_relevant_sessions(
                    client, body.pitch
                )
                yield {
                    "event": "memory_loaded",
                    "data": json.dumps({"session_ids": session_ids}),
                }

                for round_num in (1, 2):
                    for persona in PERSONAS:
                        name = persona["name"]
                        yield {
                            "event": "agent_speaking",
                            "data": json.dumps({"agent": name, "round": round_num}),
                        }

                        system, user = build_prompt(
                            persona, body.pitch, transcript, round_num, dossier
                        )
                        chunks: list[str] = []
                        turn_error: Exception | None = None
                        try:
                            async for text in stream_claude_text(
                                client, system, user, model
                            ):
                                chunks.append(text)
                                yield {
                                    "event": "token",
                                    "data": json.dumps(
                                        {
                                            "agent": name,
                                            "round": round_num,
                                            "token": text,
                                        }
                                    ),
                                }
                        except Exception as e:
                            # Don't let one failed turn freeze the UI. Fall through
                            # to finalize this turn (agent_done clears the caret and
                            # keeps the partial text), then surface why and stop.
                            turn_error = e
                            print(f"[council] turn failed ({name} r{round_num}): {e}")

                        transcript.append(
                            {
                                "agent": name,
                                "round": round_num,
                                "content": "".join(chunks),
                            }
                        )
                        yield {
                            "event": "agent_done",
                            "data": json.dumps({"agent": name, "round": round_num}),
                        }

                        if turn_error is not None:
                            yield {
                                "event": "error",
                                "data": json.dumps(
                                    {"message": _stream_error_text(turn_error)}
                                ),
                            }
                            return

            yield {"event": "discussion_complete", "data": json.dumps({})}
        except Exception as e:
            yield {"event": "error", "data": json.dumps({"message": str(e)})}

    return EventSourceResponse(event_stream())


@app.post("/wrap")
async def wrap(body: WrapBody) -> dict:
    model = resolve_model(body.model)
    user_message = (
        f"PITCH:\n{body.pitch}\n\n"
        f"TRANSCRIPT:\n{format_transcript_for_wrap(body.transcript)}\n\n"
        "Produce two outputs:\n"
        "(1) SYNTHESIS: 3-5 sentences capturing the council's collective view, "
        "including disagreements.\n"
        "(2) SUMMARY: one sentence for memory recall.\n\n"
        'Format as JSON: {"synthesis": "...", "summary": "..."}. '
        "JSON only, no preamble."
    )
    system = (
        "You are the council secretary. Below is a transcript of a council discussion."
    )

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            raw = await claude_chat(
                client, system, user_message, model, MAX_TOKENS_WRAP
            )
    except httpx.ConnectError as e:
        raise HTTPException(503, "Cannot reach the Anthropic API.") from e
    except httpx.TimeoutException as e:
        raise HTTPException(504, "Synthesis timed out.") from e
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            502, f"Anthropic returned HTTP {e.response.status_code}"
        ) from e

    result = parse_synthesis(raw)

    # Persist the session. Store the transcript in the exact {agent, round,
    # content} shape the frontend produces, so replay round-trips cleanly.
    transcript_json = json.dumps([e.model_dump() for e in body.transcript])
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (pitch, transcript_json, synthesis, summary) "
            "VALUES (?, ?, ?, ?)",
            (body.pitch, transcript_json, result["synthesis"], result["summary"]),
        )
        session_id = cur.lastrowid

    return {**result, "session_id": session_id}


@app.get("/models")
async def list_models() -> dict:
    """The picker's options and default. The allowlist is the single source of
    truth; the frontend builds its dropdown from this."""
    return {
        "models": [{"id": m, "label": label} for m, label in MODEL_CHOICES.items()],
        "default": DEFAULT_MODEL,
    }


@app.get("/sessions")
async def list_sessions() -> list[dict]:
    """Archive index, newest first. id DESC breaks ties within the same second."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, pitch, summary, created_at FROM sessions "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/sessions/{session_id}")
async def get_session(session_id: int) -> dict:
    with _db() as conn:
        row = conn.execute(
            "SELECT id, pitch, transcript_json, synthesis, summary, created_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Session not found")
    data = dict(row)
    data["transcript"] = json.loads(data.pop("transcript_json"))
    return data
