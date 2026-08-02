"""Thin HTTP client for the Meeting Copilot backend. This is the ONLY place
the exe talks to "the brain" - no OpenAI key or prompts live in the client
at all. The username/password you were given can be exchanged for a session
exactly ONCE (login() below); every call after that authenticates with the
session token it returns, and the server re-checks that session (including
expiry/revocation) on every single request."""

import base64

import requests

SERVER_URL: str | None = None
_username: str | None = None
_password: str | None = None
_session_token: str | None = None

# Render's free tier can cold-start in ~30-50s if the server has been idle -
# generous timeouts avoid a slow-wake being mistaken for a hard failure.
_TIMEOUT = 60


class AuthError(Exception):
    """Credentials are missing, wrong, expired, revoked, or already used to
    sign in once before (HTTP 401/403)."""


def configure(server_url: str, username: str, password: str):
    global SERVER_URL, _username, _password
    SERVER_URL = server_url.rstrip("/")
    _username = username
    _password = password


def _post(path: str, json_body: dict) -> dict:
    if not SERVER_URL or not _session_token:
        raise RuntimeError("not logged in yet - call login() first")
    try:
        resp = requests.post(
            f"{SERVER_URL}{path}",
            json=json_body,
            headers={"X-Session-Token": _session_token},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"could not reach server: {e}") from e

    if resp.status_code == 401:
        detail = resp.json().get("detail", "unauthorized") if resp.content else "unauthorized"
        raise AuthError(detail)
    resp.raise_for_status()
    return resp.json()


def login() -> float:
    """Exchanges the configured username/password for a session token - this
    can only succeed ONCE per credential. Returns the credential's
    expires_at (unix timestamp) on success. Raises AuthError on bad/expired/
    revoked/already-used credentials."""
    global _session_token
    if not SERVER_URL or not _username or not _password:
        raise RuntimeError("api_client.configure() was never called")
    try:
        resp = requests.post(
            f"{SERVER_URL}/login", auth=(_username, _password), timeout=_TIMEOUT
        )
    except requests.RequestException as e:
        raise RuntimeError(f"could not reach server: {e}") from e
    if resp.status_code in (401, 403):
        detail = resp.json().get("detail", "unauthorized") if resp.content else "unauthorized"
        raise AuthError(detail)
    resp.raise_for_status()
    data = resp.json()
    _session_token = data["session_token"]
    return data["expires_at"]


def check_session():
    """Re-validates the existing session (expiry/revocation) WITHOUT
    consuming a new login - safe to call repeatedly for a periodic
    background health check, unlike login()."""
    if not SERVER_URL or not _session_token:
        raise RuntimeError("not logged in yet - call login() first")
    try:
        resp = requests.get(
            f"{SERVER_URL}/session-status",
            headers={"X-Session-Token": _session_token},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"could not reach server: {e}") from e
    if resp.status_code == 401:
        detail = resp.json().get("detail", "unauthorized") if resp.content else "unauthorized"
        raise AuthError(detail)
    resp.raise_for_status()


def analyze_text(snippet: str, source: str) -> str | None:
    return _post("/analyze-text", {"snippet": snippet, "source": source})["answer"]


def analyze_audio(wav_bytes: bytes, source: str) -> str | None:
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    return _post("/analyze-audio", {"wav_b64": b64, "source": source})["answer"]


def transcribe(wav_bytes: bytes) -> str:
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    return _post("/transcribe", {"wav_b64": b64})["text"]


def answer_query(query: str) -> str | None:
    return _post("/answer-query", {"query": query})["answer"]


def extract_screen(jpeg_bytes: bytes) -> str | None:
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    return _post("/extract-screen", {"image_b64": b64})["text"]
