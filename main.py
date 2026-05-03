import json
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen3.6:latest"

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

app = FastAPI()


class PitchBody(BaseModel):
    pitch: str = Field(..., min_length=1)


class TranscriptEntry(BaseModel):
    agent: str
    round: int
    content: str


class WrapBody(BaseModel):
    pitch: str = Field(..., min_length=1)
    transcript: list[TranscriptEntry] = Field(..., min_length=1)


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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "templates" / "index.html")


@app.get("/health/ollama")
async def health_ollama() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{OLLAMA_URL}/api/version")
        resp.raise_for_status()
        return resp.json()


ROUND2_SYSTEM_SUFFIX = (
    "\n\nYou have heard the others speak. "
    "Engage with specific points they made. "
    "Do not just restate your view."
)


def build_messages(
    persona: dict, pitch: str, prior: list[dict], round_num: int
) -> list[dict]:
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
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


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

                        payload = {
                            "model": MODEL,
                            "stream": True,
                            "think": False,
                            "options": {"num_ctx": 16384},
                            "messages": build_messages(
                                persona, body.pitch, transcript, round_num
                            ),
                        }

                        chunks: list[str] = []
                        async with client.stream(
                            "POST", f"{OLLAMA_URL}/api/chat", json=payload
                        ) as resp:
                            resp.raise_for_status()
                            async for line in resp.aiter_lines():
                                if not line:
                                    continue
                                chunk = json.loads(line)
                                token = chunk.get("message", {}).get("content", "")
                                if token:
                                    chunks.append(token)
                                    yield {
                                        "event": "token",
                                        "data": json.dumps(
                                            {
                                                "agent": name,
                                                "round": round_num,
                                                "token": token,
                                            }
                                        ),
                                    }
                                if chunk.get("done"):
                                    break

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

    payload = {
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {"num_ctx": 16384},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the council secretary. "
                    "Below is a transcript of a council discussion."
                ),
            },
            {"role": "user", "content": user_message},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            raw = resp.json().get("message", {}).get("content", "")
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot reach Ollama at localhost:11434. Is it running?")
    except httpx.TimeoutException:
        raise HTTPException(504, "Synthesis timed out.")
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Ollama returned HTTP {e.response.status_code}")

    return parse_synthesis(raw)
