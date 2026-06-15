import json
import os
import secrets
import sqlite3
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

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TOKENS_AGENT = 1024
MAX_TOKENS_WRAP = 1024

# ─────────── Auth (HTTP Basic, gated by env vars) ───────────
COUNCIL_USER = os.environ.get("COUNCIL_USER")
COUNCIL_PASSWORD = os.environ.get("COUNCIL_PASSWORD")
_basic = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
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


class TranscriptEntry(BaseModel):
    agent: str
    round: int
    content: str


class WrapBody(BaseModel):
    pitch: str = Field(..., min_length=1)
    transcript: list[TranscriptEntry] = Field(..., min_length=1)


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


def _claude_payload(system: str, user: str, *, stream: bool, max_tokens: int) -> dict:
    return {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "stream": stream,
    }


async def stream_claude_text(client: httpx.AsyncClient, system: str, user: str):
    """Yields text chunks from a streaming /v1/messages call."""
    payload = _claude_payload(system, user, stream=True, max_tokens=MAX_TOKENS_AGENT)
    async with client.stream(
        "POST", ANTHROPIC_URL, headers=_anthropic_headers(), json=payload
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if chunk.get("type") == "content_block_delta":
                delta = chunk.get("delta") or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        yield text
            elif chunk.get("type") == "message_stop":
                return


async def claude_chat(
    client: httpx.AsyncClient, system: str, user: str, max_tokens: int
) -> str:
    """Non-streaming /v1/messages call. Returns concatenated text content."""
    payload = _claude_payload(system, user, stream=False, max_tokens=max_tokens)
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
    persona: dict, pitch: str, prior: list[dict], round_num: int
) -> tuple[str, str]:
    """Returns (system, user) for the Anthropic API."""
    system = persona["system"]
    if round_num == 2:
        system += ROUND2_SYSTEM_SUFFIX

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


# ─────────── Routes ───────────
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "templates" / "index.html")


@app.post("/pitch")
async def pitch(body: PitchBody) -> EventSourceResponse:
    async def event_stream():
        transcript: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                for round_num in (1, 2):
                    for persona in PERSONAS:
                        name = persona["name"]
                        yield {
                            "event": "agent_speaking",
                            "data": json.dumps({"agent": name, "round": round_num}),
                        }

                        system, user = build_prompt(
                            persona, body.pitch, transcript, round_num
                        )
                        chunks: list[str] = []
                        async for text in stream_claude_text(client, system, user):
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

            yield {"event": "discussion_complete", "data": json.dumps({})}
        except Exception as e:
            yield {"event": "error", "data": json.dumps({"message": str(e)})}

    return EventSourceResponse(event_stream())


@app.post("/wrap")
async def wrap(body: WrapBody) -> dict:
    user_message = (
        f"PITCH:\n{body.pitch}\n\n"
        f"TRANSCRIPT:\n{format_transcript_for_wrap(body.transcript)}\n\n"
        "Produce two outputs:\n"
        "(1) SYNTHESIS: 3–5 sentences capturing the council's collective view, "
        "including disagreements.\n"
        "(2) SUMMARY: one sentence for memory recall.\n\n"
        'Format as JSON: {"synthesis": "...", "summary": "..."}. '
        "JSON only, no preamble."
    )
    system = (
        "You are the council secretary. "
        "Below is a transcript of a council discussion."
    )

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            raw = await claude_chat(client, system, user_message, MAX_TOKENS_WRAP)
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot reach the Anthropic API.")
    except httpx.TimeoutException:
        raise HTTPException(504, "Synthesis timed out.")
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Anthropic returned HTTP {e.response.status_code}")

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
