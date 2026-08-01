"""Meeting Copilot backend - the only place the real OpenAI key and prompts
live. The client exe never sees either: it authenticates with a
username/password you issue, and every request is re-validated (including
expiry) against the credential store below, not just checked once at login.

Run locally:   uvicorn app:app --reload --port 8000
Deploy target:  Render.com (or any host that runs a standard ASGI app)

Environment variables required:
  OPENAI_API_KEY  - your real OpenAI key, set only here, never in the client
  ADMIN_SECRET    - a long random string only you know, guards /admin/* routes
"""

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from openai import OpenAI
from pydantic import BaseModel

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ADMIN_SECRET = os.environ["ADMIN_SECRET"]

_client = OpenAI(api_key=OPENAI_API_KEY)

TOKENS_PATH = Path(__file__).parent / "tokens.json"

app = FastAPI(title="Meeting Copilot Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # admin routes are gated by ADMIN_SECRET, not CORS
    allow_methods=["*"],
    allow_headers=["*"],
)
security = HTTPBasic()


# ---------- credential store ----------

def _load_tokens() -> dict:
    if not TOKENS_PATH.exists():
        return {}
    return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))


def _save_tokens(tokens: dict):
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Re-validated on EVERY request, not just at login - an expired or
    revoked credential stops working immediately, even mid-session."""
    tokens = _load_tokens()
    entry = tokens.get(credentials.username)
    if not entry or entry.get("revoked"):
        raise HTTPException(status_code=401, detail="invalid credentials")
    if _hash_password(credentials.password, entry["salt"]) != entry["password_hash"]:
        raise HTTPException(status_code=401, detail="invalid credentials")
    if time.time() > entry["expires_at"]:
        raise HTTPException(status_code=401, detail="credential expired")
    return credentials.username


def require_admin(x_admin_secret: str = Header(default="")):
    if not secrets.compare_digest(x_admin_secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="invalid admin secret")


# ---------- shared QA logic (ported from the client's qa_engine.py) ----------

TECH_CONTEXT_NOTE = """Always interpret ambiguous words or snippets through a computer \
science / programming lens FIRST, since that's the context you're in - even if the same \
word has a common, everyday, or other-field meaning. For example, "SOLID" almost always \
means the five OOP design principles (Single Responsibility, Open/Closed, Liskov \
Substitution, Interface Segregation, Dependency Inversion), NOT the physical state of \
matter. The same applies to other overloaded CS terms - e.g. tree, stack, queue, heap, \
class, object, thread, process, socket, cache, kernel, virus, bug, table, key, lock, \
session, container - answer using their technical/programming meaning, not the literal \
everyday one."""

ANSWER_STYLE_INSTRUCTIONS = """Respond with a short, well-defined answer written EXACTLY \
as the person should SAY it out loud, in natural spoken first-person language - not notes, \
not bullet points, not headers. Keep it tight and proper: a few clear sentences, never a \
wall of text.

- Technical/conceptual question: a clear, confident spoken explanation - what it is, how it \
works, and a brief example if it helps - like actually answering the interviewer out loud.
- Coding problem or stub to implement: briefly explain the approach in spoken language, \
then include a CODE section with the complete, correct implementation.
- Error/exception/failing test: explain in one or two spoken sentences what's wrong and \
why, then include a CODE section with the corrected version.
- Behavioral/situational question ("tell me about a time..."): structure the content using \
the STAR method (Situation, Task, Action, Result) internally, but write it as flowing \
natural spoken sentences, not labeled sections - a ready-to-say ~30-45 second answer.

Reply in this exact format, nothing else:
Q: <question in one short line>
SAY: <the natural spoken answer - well-defined, proper, not too long>
CODE:
<only if code-related: the complete, correct implementation. Omit this section entirely \
if the question isn't code-related.>
"""

SYSTEM_PROMPT = f"""You help someone live in a TECHNICAL job interview or study/exam \
session by watching/listening for questions (via overheard audio or on-screen text). \
Their questions are almost always tech and study related: programming, computer science \
concepts, system design, coding problems, or academic/exam material - plus the occasional \
behavioral/situational interview question. For each snippet, decide if it contains a real \
question worth answering in that scope - technical, behavioral, situational, or a coding \
problem/stub (an empty function/class stub counts as "implement this" even with no \
question sentence written) - or an error/exception/failing test that needs fixing.

Do NOT answer things outside that scope: casual small talk, greetings, meeting logistics \
("can everyone hear me", "let's get started"), or any topic with no technical/study/\
interview relevance - treat these as NONE even if phrased as a question.

{TECH_CONTEXT_NOTE}

{ANSWER_STYLE_INSTRUCTIONS}
If there is NEITHER a real technical/study question NOR an error, reply with exactly: NONE
"""

SEARCH_SYSTEM_PROMPT = f"""You help someone live in a technical job interview or study/exam \
session who just asked a question directly - typed or spoken on purpose - so ALWAYS answer \
it, never reply NONE. Assume it's tech/study related (programming, CS concepts, system \
design, coding problems, academic material) unless it clearly isn't.

{TECH_CONTEXT_NOTE}

{ANSWER_STYLE_INSTRUCTIONS}
"""

EXTRACT_PROMPT = (
    "You are looking at a screenshot of the user's screen during a meeting. "
    "Transcribe verbatim ANY of the following if visible: "
    "(1) a question, coding problem, or exam/quiz question written in words; "
    "(2) an error message, exception, stack trace, compiler error, failing "
    "test output, or lint error (e.g. in a terminal, IDE, or browser console); "
    "(3) a coding problem stub/template to implement (e.g. a LeetCode/HackerRank "
    "-style empty function or class signature, or method left unimplemented), "
    "even if no question sentence is written - include the problem title and "
    "any visible description text alongside the signature. "
    "If an error is visible AND the related source code is also visible "
    "(e.g. an editor pane behind/near the terminal), transcribe the FULL "
    "source code verbatim first under a 'CODE:' label, then the error text "
    "under an 'ERROR:' label - this is important so the mistake can be "
    "explained precisely. "
    "Ignore ordinary UI chrome, menus, and unrelated content. "
    "If none of the above is visible, reply with exactly: NONE."
)

AUDIO_QA_MODEL = "gpt-audio"
TRANSCRIBE_MODEL = "whisper-1"
VISION_MODEL = "gpt-4o-mini"
QA_MODEL = "gpt-4o-mini"


class TextRequest(BaseModel):
    snippet: str
    source: str = "audio"


class QueryRequest(BaseModel):
    query: str


class AudioRequest(BaseModel):
    wav_b64: str
    source: str = "audio"


class ImageRequest(BaseModel):
    image_b64: str  # jpeg


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/login")
def login(username: str = Depends(require_auth)):
    tokens = _load_tokens()
    return {"ok": True, "expires_at": tokens[username]["expires_at"]}


@app.post("/analyze-text")
def analyze_text(req: TextRequest, _user: str = Depends(require_auth)):
    if not req.snippet or len(req.snippet.strip()) < 3:
        return {"answer": None}
    resp = _client.chat.completions.create(
        model=QA_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"[{req.source}] {req.snippet}"},
        ],
        max_tokens=220,
        temperature=0.3,
    )
    text = (resp.choices[0].message.content or "").strip()
    return {"answer": None if text.upper() == "NONE" or not text else text}


def _transcribe_and_analyze(wav_bytes: bytes, source: str) -> str | None:
    """Fallback path: separate Whisper transcription + text analysis. Slower
    (~2 calls) but works even if the audio-direct model is unavailable."""
    import io
    buf = io.BytesIO(wav_bytes)
    buf.name = "utterance.wav"
    result = _client.audio.transcriptions.create(model=TRANSCRIBE_MODEL, file=buf)
    text = (result.text or "").strip()
    if not text:
        return None
    resp = _client.chat.completions.create(
        model=QA_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"[{source}] {text}"},
        ],
        max_tokens=220,
        temperature=0.3,
    )
    answer = (resp.choices[0].message.content or "").strip()
    return None if answer.upper() == "NONE" or not answer else answer


@app.post("/analyze-audio")
def analyze_audio(req: AudioRequest, _user: str = Depends(require_auth)):
    wav_bytes = base64.b64decode(req.wav_b64)
    if not wav_bytes:
        return {"answer": None}
    try:
        resp = _client.chat.completions.create(
            model=AUDIO_QA_MODEL,
            modalities=["text"],
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": f"[{req.source}] (snippet attached as audio)"},
                    {"type": "input_audio", "input_audio": {"data": req.wav_b64, "format": "wav"}},
                ]},
            ],
            max_tokens=220,
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        return {"answer": None if text.upper() == "NONE" or not text else text}
    except Exception as e:
        print(f"[server] direct audio QA failed ({e}), falling back to transcribe+analyze")
        return {"answer": _transcribe_and_analyze(wav_bytes, req.source)}


@app.post("/transcribe")
def transcribe(req: AudioRequest, _user: str = Depends(require_auth)):
    import io
    wav_bytes = base64.b64decode(req.wav_b64)
    buf = io.BytesIO(wav_bytes)
    buf.name = "utterance.wav"
    try:
        result = _client.audio.transcriptions.create(model=TRANSCRIBE_MODEL, file=buf)
        return {"text": (result.text or "").strip()}
    except Exception as e:
        return {"text": f"[transcription error: {e}]"}


@app.post("/answer-query")
def answer_query(req: QueryRequest, _user: str = Depends(require_auth)):
    if not req.query or not req.query.strip():
        return {"answer": None}
    resp = _client.chat.completions.create(
        model=QA_MODEL,
        messages=[
            {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": req.query.strip()},
        ],
        max_tokens=220,
        temperature=0.3,
    )
    text = (resp.choices[0].message.content or "").strip()
    return {"answer": text or None}


@app.post("/extract-screen")
def extract_screen(req: ImageRequest, _user: str = Depends(require_auth)):
    resp = _client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": EXTRACT_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{req.image_b64}"}},
            ],
        }],
        max_tokens=300,
    )
    text = (resp.choices[0].message.content or "").strip()
    return {"text": None if text.upper() == "NONE" or not text else text}


# ---------- admin: issue/list/revoke credentials ----------

class CreateTokenRequest(BaseModel):
    username: str
    password: str
    hours: float
    label: str = ""


class UpdateTokenRequest(BaseModel):
    revoked: bool | None = None       # set True to deactivate, False to reactivate
    extend_hours: float | None = None  # if set, expiry becomes now + extend_hours
    label: str | None = None


@app.post("/admin/tokens")
def create_token(req: CreateTokenRequest, _admin=Depends(require_admin)):
    tokens = _load_tokens()
    salt = secrets.token_hex(16)
    tokens[req.username] = {
        "password_hash": _hash_password(req.password, salt),
        "salt": salt,
        "expires_at": time.time() + req.hours * 3600,
        "label": req.label,
        "created_at": time.time(),
        "revoked": False,
    }
    _save_tokens(tokens)
    return {"ok": True, "username": req.username, "expires_at": tokens[req.username]["expires_at"]}


@app.get("/admin/tokens")
def list_tokens(_admin=Depends(require_admin)):
    tokens = _load_tokens()
    return {
        username: {
            "label": entry["label"],
            "expires_at": entry["expires_at"],
            "created_at": entry["created_at"],
            "revoked": entry.get("revoked", False),
        }
        for username, entry in tokens.items()
    }


@app.patch("/admin/tokens/{username}")
def update_token(username: str, req: UpdateTokenRequest, _admin=Depends(require_admin)):
    """Deactivate/reactivate a credential, extend its expiry, or relabel it -
    without needing to delete and recreate it (which would also reset its
    password)."""
    tokens = _load_tokens()
    if username not in tokens:
        raise HTTPException(status_code=404, detail="not found")
    if req.revoked is not None:
        tokens[username]["revoked"] = req.revoked
    if req.extend_hours is not None:
        tokens[username]["expires_at"] = time.time() + req.extend_hours * 3600
    if req.label is not None:
        tokens[username]["label"] = req.label
    _save_tokens(tokens)
    entry = tokens[username]
    return {
        "ok": True,
        "username": username,
        "expires_at": entry["expires_at"],
        "revoked": entry["revoked"],
        "label": entry["label"],
    }


@app.delete("/admin/tokens/{username}")
def delete_token(username: str, _admin=Depends(require_admin)):
    """Permanently remove a credential. To just stop it working (and keep
    the record around, reversibly), use PATCH with revoked=true instead."""
    tokens = _load_tokens()
    if username not in tokens:
        raise HTTPException(status_code=404, detail="not found")
    del tokens[username]
    _save_tokens(tokens)
    return {"ok": True}
