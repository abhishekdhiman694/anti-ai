"""Thin HTTP client for the Meeting Copilot backend. This is the ONLY place
the exe talks to "the brain" - no OpenAI key or prompts live in the client
at all; every call is authenticated with the username/password you were
given, and the server re-checks that credential (including expiry) on every
single request, not just once at login."""

import base64

import requests

SERVER_URL: str | None = None
_username: str | None = None
_password: str | None = None

# Render's free tier can cold-start in ~30-50s if the server has been idle -
# generous timeouts avoid a slow-wake being mistaken for a hard failure.
_TIMEOUT = 60


class AuthError(Exception):
    """Credentials are missing, wrong, expired, or revoked (HTTP 401)."""


def configure(server_url: str, username: str, password: str):
    global SERVER_URL, _username, _password
    SERVER_URL = server_url.rstrip("/")
    _username = username
    _password = password


def _post(path: str, json_body: dict) -> dict:
    if not SERVER_URL or not _username or not _password:
        raise RuntimeError("api_client.configure() was never called")
    try:
        resp = requests.post(
            f"{SERVER_URL}{path}",
            json=json_body,
            auth=(_username, _password),
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
    """Returns the credential's expires_at (unix timestamp) on success.
    Raises AuthError on bad/expired/revoked credentials."""
    if not SERVER_URL or not _username or not _password:
        raise RuntimeError("api_client.configure() was never called")
    try:
        resp = requests.post(
            f"{SERVER_URL}/login", auth=(_username, _password), timeout=_TIMEOUT
        )
    except requests.RequestException as e:
        raise RuntimeError(f"could not reach server: {e}") from e
    if resp.status_code == 401:
        detail = resp.json().get("detail", "unauthorized") if resp.content else "unauthorized"
        raise AuthError(detail)
    resp.raise_for_status()
    return resp.json()["expires_at"]


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
