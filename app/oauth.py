"""One-click "Sign in with Google / Microsoft" for connecting mailboxes, calendars and files.

Flow:
1. Admin clicks "Connect Google account" -> /api/oauth/start creates a pending request and returns
   the provider's sign-in URL.
2. The user approves on Google's / Microsoft's own page.
3. The provider sends the browser to the redirect URL. Customer servers have different addresses,
   so the redirect URL is normally a small page on the vendor's domain (relay/oauth.html) that
   forwards the browser back to this installation's /oauth/callback. When testing on
   http://localhost the provider can send the browser here directly.
4. /oauth/callback exchanges the code for tokens, stores them encrypted, and creates connectors.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

import httpx

from .config import settings
from .connectors.base import decrypt, encrypt
from .db import conn

GOOGLE_SCOPES = {
    "mail": "https://www.googleapis.com/auth/gmail.readonly",
    "calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "files": "https://www.googleapis.com/auth/drive.readonly",
}
MICROSOFT_SCOPES = {"mail": "Mail.Read", "calendar": "Calendars.Read", "files": "Files.Read.All"}

PROVIDERS = {
    "google": {
        "name": "Google",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "base_scopes": ["openid", "email"],
        "scopes": GOOGLE_SCOPES,
        "connector_types": {"mail": "gmail", "calendar": "gcal", "files": "gdrive"},
    },
    "microsoft": {
        "name": "Microsoft",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "base_scopes": ["openid", "email", "offline_access", "User.Read"],
        "scopes": MICROSOFT_SCOPES,
        "connector_types": {"mail": "ms_mail", "calendar": "ms_cal", "files": "ms_drive"},
    },
}
SERVICE_LABEL = {"mail": "Email", "calendar": "Calendar", "files": "Files"}


def _vendor_file() -> dict:
    p = Path(__file__).parent / "vendor_oauth.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except json.JSONDecodeError:
        return {}


def credentials(provider: str) -> tuple[str, str]:
    """Client id and secret: from .env if set, otherwise from app/vendor_oauth.json."""
    v = _vendor_file().get(provider, {})
    if provider == "google":
        return (settings.google_client_id or v.get("client_id", ""),
                settings.google_client_secret or v.get("client_secret", ""))
    return (settings.microsoft_client_id or v.get("client_id", ""),
            settings.microsoft_client_secret or v.get("client_secret", ""))


def redirect_url(origin: str) -> str:
    return settings.oauth_redirect_url or _vendor_file().get("redirect_url") or f"{origin}/oauth/callback"


def configured() -> dict:
    return {p: bool(credentials(p)[0]) for p in PROVIDERS}


def http() -> httpx.Client:
    """Single place to create HTTP clients (tests replace this)."""
    return httpx.Client(timeout=60, follow_redirects=True)


# ------------------------------------------------------------------ pending sign-ins (in memory, 15 min)

_pending: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def start(provider: str, services: list[str], department_ids: list[int], company_wide: bool, origin: str) -> str:
    if provider not in PROVIDERS:
        raise ValueError("Unknown provider")
    client_id, _ = credentials(provider)
    if not client_id:
        raise ValueError(f"{PROVIDERS[provider]['name']} sign-in is not set up on this installation yet.")
    services = [s for s in services if s in SERVICE_LABEL]
    if not services:
        raise ValueError("Choose at least one: Email, Calendar or Files")
    cfg = PROVIDERS[provider]
    verifier = _b64(secrets.token_bytes(32))
    key = secrets.token_urlsafe(24)
    with _pending_lock:
        now = time.time()
        for k in [k for k, v in _pending.items() if now - v["created"] > 900]:
            del _pending[k]
        _pending[key] = {"provider": provider, "services": services, "department_ids": department_ids,
                         "company_wide": company_wide, "verifier": verifier, "origin": origin, "created": now}
    # The state carries this installation's address so the relay page knows where to send the browser.
    state = _b64(json.dumps({"k": key, "r": origin}, separators=(",", ":")).encode())
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_url(origin),
        "scope": " ".join(cfg["base_scopes"] + [cfg["scopes"][s] for s in services]),
        "state": state,
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
        "prompt": "consent" if provider == "google" else "select_account",
    }
    if provider == "google":
        params["access_type"] = "offline"
    return str(httpx.URL(cfg["auth_url"], params=params))


def _token_request(provider: str, data: dict) -> dict:
    client_id, client_secret = credentials(provider)
    data = {**data, "client_id": client_id}
    if client_secret:
        data["client_secret"] = client_secret
    with http() as h:
        r = h.post(PROVIDERS[provider]["token_url"], data=data)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        raise RuntimeError(body.get("error_description") or body.get("error") or f"Sign-in failed ({r.status_code})")
    return body


def _account_email(provider: str, access_token: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    with http() as h:
        if provider == "google":
            r = h.get("https://openidconnect.googleapis.com/v1/userinfo", headers=headers)
            return r.json().get("email", "")
        r = h.get("https://graph.microsoft.com/v1.0/me", headers=headers)
        me = r.json()
        return me.get("mail") or me.get("userPrincipalName") or ""


def finish(state: str, code: str) -> list[int]:
    """Handle the provider's answer: save the account and create its connectors. Returns connector ids."""
    try:
        key = json.loads(base64.urlsafe_b64decode(state + "==")).get("k", "")
    except (ValueError, json.JSONDecodeError):
        raise ValueError("Invalid sign-in response")
    with _pending_lock:
        pending = _pending.pop(key, None)
    if not pending or time.time() - pending["created"] > 900:
        raise ValueError("This sign-in has expired. Please start again.")
    provider = pending["provider"]
    tokens = _token_request(provider, {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_url(pending["origin"]), "code_verifier": pending["verifier"],
    })
    if not tokens.get("refresh_token"):
        raise RuntimeError("The provider did not allow offline access. Please try again and approve all requests.")
    email = _account_email(provider, tokens["access_token"])
    token_blob = {"refresh_token": tokens["refresh_token"], "access_token": tokens["access_token"],
                  "expires_at": time.time() + int(tokens.get("expires_in", 3600)) - 60}
    created = []
    with conn() as c:
        account_id = c.execute(
            "INSERT INTO oauth_accounts(provider, email, tokens) VALUES (%s, %s, %s) RETURNING id",
            (provider, email, encrypt(json.dumps(token_blob))),
        ).fetchone()["id"]
        for service in pending["services"]:
            ctype = PROVIDERS[provider]["connector_types"][service]
            cid = c.execute(
                "INSERT INTO connectors(type, name, config, company_wide) VALUES (%s, %s, %s, %s) RETURNING id",
                (ctype, f"{SERVICE_LABEL[service]} – {email}", json.dumps({"account_id": account_id}),
                 pending["company_wide"]),
            ).fetchone()["id"]
            for d in set(pending["department_ids"]):
                c.execute("INSERT INTO connector_departments VALUES (%s, %s)", (cid, d))
            created.append(cid)
    return created


# ------------------------------------------------------------------ access tokens for connectors

_refresh_lock = threading.Lock()


def access_token(account_id: int) -> str:
    with _refresh_lock:
        with conn() as c:
            row = c.execute("SELECT provider, tokens FROM oauth_accounts WHERE id=%s", (account_id,)).fetchone()
        if not row:
            raise RuntimeError("The connected account was removed. Connect it again.")
        tokens = json.loads(decrypt(row["tokens"]))
        if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time():
            return tokens["access_token"]
        try:
            fresh = _token_request(row["provider"], {"grant_type": "refresh_token",
                                                     "refresh_token": tokens["refresh_token"]})
        except RuntimeError as e:
            raise RuntimeError(f"Access was revoked or expired ({e}). Remove this connection and connect the account again.")
        tokens["access_token"] = fresh["access_token"]
        tokens["expires_at"] = time.time() + int(fresh.get("expires_in", 3600)) - 60
        if fresh.get("refresh_token"):  # Microsoft rotates refresh tokens
            tokens["refresh_token"] = fresh["refresh_token"]
        with conn() as c:
            c.execute("UPDATE oauth_accounts SET tokens=%s WHERE id=%s", (encrypt(json.dumps(tokens)), account_id))
        return tokens["access_token"]


class Api:
    """Small authenticated JSON/bytes client for Google and Microsoft Graph APIs."""

    def __init__(self, account_id: int):
        self.account_id = account_id
        self.client = http()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.client.close()

    def _request(self, url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        for attempt in range(4):
            h = {"Authorization": f"Bearer {access_token(self.account_id)}", **(headers or {})}
            r = self.client.get(url, params=params, headers=h)
            if r.status_code in (429, 503) and attempt < 3:
                time.sleep(min(int(r.headers.get("retry-after", "2") or 2), 30))
                continue
            if r.status_code == 401 and attempt == 0:
                with conn() as c:  # force a token refresh once
                    row = c.execute("SELECT tokens FROM oauth_accounts WHERE id=%s", (self.account_id,)).fetchone()
                    t = json.loads(decrypt(row["tokens"]))
                    t["expires_at"] = 0
                    c.execute("UPDATE oauth_accounts SET tokens=%s WHERE id=%s", (encrypt(json.dumps(t)), self.account_id))
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("error", {})
                    msg = msg.get("message") if isinstance(msg, dict) else msg
                except ValueError:
                    msg = r.text[:200]
                raise RuntimeError(f"{r.status_code}: {msg}")
            return r
        raise RuntimeError("The service is busy, will retry at the next sync")

    def json(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        return self._request(url, params, headers).json()

    def bytes(self, url: str, params: dict | None = None) -> bytes:
        return self._request(url, params).content
