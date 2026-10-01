"""
switchboard_ai.auth
───────────────
Direct authentication and token extraction for Claude Code and Antigravity.

Extracts:
  - Claude: OAuth access token (sk-ant-oat01-...), refresh token (sk-ant-ort01-...),
    expiration timestamps, subscription type, and organization UUID from
    ~/.claude/.credentials.json.
  - Antigravity: Google OAuth access token (ya29...), refresh token (1//...),
    API key, user name, email, and subscription plan from the Antigravity
    state database (<app data>/Antigravity/User/globalStorage/state.vscdb, where
    <app data> is %APPDATA% on Windows, ~/Library/Application Support on macOS
    and ~/.config on Linux).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.parse

from switchboard_ai import config
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _write_private(path: Path, text: str) -> None:
    """Write a credentials file readable only by the current user (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)  # also tighten a file that already existed
    except OSError:
        pass


def _encode_varint(n: int) -> bytes:
    res = bytearray()
    while n > 0x7F:
        res.append((n & 0x7F) | 0x80)
        n >>= 7
    res.append(n & 0x7F)
    return bytes(res)


def _encode_field(tag: int, val_bytes: bytes) -> bytes:
    key = (tag << 3) | 2
    return _encode_varint(key) + _encode_varint(len(val_bytes)) + val_bytes


def _pack_oauth_token_pb(access_token: str, refresh_token: str = "", expiry_secs: int = 3600) -> str:
    """Pack OAuth tokens into the protobuf base64 format stored in state.vscdb."""
    expiry_ts = int(time.time() + expiry_secs)
    f1 = _encode_field(1, access_token.encode("ascii"))
    f2 = _encode_field(2, b"Bearer")
    f3 = _encode_field(3, refresh_token.encode("ascii")) if refresh_token else b""
    f4_body = _encode_varint((1 << 3) | 0) + _encode_varint(expiry_ts)
    f4 = _encode_field(4, f4_body)
    tok_proto = f1 + f2 + f3 + f4
    b64_inner = base64.b64encode(tok_proto)

    context_json = json.dumps({
        "state": "signedIn",
        "context": {
            "project": "",
            "showProjectError": False,
            "errorMessage": "",
            "ineligibleMessage": "",
            "verificationUrl": "",
            "isGcpTos": False,
            "browserOpenFailed": False,
            "appealUrl": "",
            "appealLinkText": "",
        },
    }).encode("utf-8")
    f_ctx_val = _encode_field(1, context_json)
    f_ctx = _encode_field(1, b"authStateWithContextSentinelKey") + _encode_field(2, f_ctx_val)

    f_tok_val = _encode_field(1, b64_inner)
    f_tok = _encode_field(1, b"oauthTokenInfoSentinelKey") + _encode_field(2, f_tok_val)

    wrapper = _encode_field(1, f_ctx) + _encode_field(2, f_tok)
    return base64.b64encode(wrapper).decode("ascii")



def mask_token(token: Optional[str], keep_prefix: int = 12, keep_suffix: int = 6) -> str:
    """Safely mask sensitive tokens for display/logs, e.g. sk-ant-oat01-***DV5m1gAA."""
    if not token:
        return ""
    if len(token) <= (keep_prefix + keep_suffix + 3):
        return "***"
    return f"{token[:keep_prefix]}***{token[-keep_suffix:]}"


def _format_expiry(timestamp_ms: Optional[int]) -> dict[str, Any]:
    """Calculate expiration status and human-readable time."""
    if not timestamp_ms:
        return {"expires_at": None, "is_expired": False, "expires_in": "unknown"}
    now_ms = int(time.time() * 1000)
    diff_ms = timestamp_ms - now_ms
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
    if diff_ms <= 0:
        return {"expires_at": dt, "is_expired": True, "expires_in": "expired"}
    
    hours = diff_ms / (1000 * 3600)
    if hours < 1:
        minutes = int(diff_ms / (1000 * 60))
        human = f"{minutes}m"
    elif hours < 48:
        human = f"{round(hours, 1)}h"
    else:
        days = round(hours / 24, 1)
        human = f"{days}d"
    return {"expires_at": dt, "is_expired": False, "expires_in": human}


class ClaudeAuth:
    """Handles credentials and pure-Python OAuth for Claude (Direct API, zero claude.exe)."""

    CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
    CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    AUTH_URL = "https://claude.com/cai/oauth/authorize"
    TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
    PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
    REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
    SCOPES = [
        "org:create_api_key",
        "user:profile",
        "user:inference",
        "user:sessions:claude_code",
        "user:mcp_servers",
        "user:file_upload",
        "user:plugins",
    ]

    _cached_email: Optional[str] = None
    _pending_oauth: dict[str, str] = {}
    _latest_verifier: Optional[str] = None
    _latest_state: Optional[str] = None

    @classmethod
    def generate_pkce(cls) -> tuple[str, str]:
        """Generate RFC 7636 PKCE code_verifier and code_challenge (S256)."""
        verifier = secrets.token_urlsafe(32)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    @classmethod
    def get_email(cls) -> Optional[str]:
        if cls._cached_email:
            return cls._cached_email
        if cls.CREDENTIALS_PATH.is_file():
            try:
                data = json.loads(cls.CREDENTIALS_PATH.read_text(encoding="utf-8"))
                email = data.get("email") or data.get("account", {}).get("email_address")
                if email:
                    cls._cached_email = email
                    return email
            except Exception:
                pass
        return None

    @classmethod
    def is_present(cls) -> bool:
        return cls.CREDENTIALS_PATH.is_file()

    @classmethod
    def get_credentials(cls, reveal: bool = False) -> dict[str, Any]:
        """Read and parse ~/.claude/.credentials.json."""
        if not cls.CREDENTIALS_PATH.is_file():
            return {
                "available": False,
                "provider": "claude",
                "label": "Claude (Direct API)",
                "logged_in": False,
                "error": "No .credentials.json found in ~/.claude/",
            }

        try:
            raw = cls.CREDENTIALS_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as e:
            return {
                "available": True,
                "provider": "claude",
                "label": "Claude (Direct API)",
                "logged_in": False,
                "error": f"Failed to parse .credentials.json: {e}",
            }

        oauth = data.get("claudeAiOauth", {})
        access_tok = oauth.get("accessToken", "")
        refresh_tok = oauth.get("refreshToken", "")
        exp_ms = oauth.get("expiresAt")
        rf_exp_ms = oauth.get("refreshTokenExpiresAt")
        subscription = oauth.get("subscriptionType", "pro")

        access_exp = _format_expiry(exp_ms)
        refresh_exp = _format_expiry(rf_exp_ms)

        logged_in = bool(access_tok and not refresh_exp["is_expired"])
        email = cls.get_email() or data.get("email")

        return {
            "available": True,
            "provider": "claude",
            "label": "Claude (Direct API)",
            "logged_in": logged_in,
            "email": email,
            "subscription": subscription,
            "rate_limit_tier": oauth.get("rateLimitTier", "default"),
            "organization_uuid": data.get("organizationUuid"),
            "scopes": oauth.get("scopes", []),
            "access_token": access_tok if reveal else mask_token(access_tok, 14, 8),
            "refresh_token": refresh_tok if reveal else mask_token(refresh_tok, 14, 8),
            "access_token_expires_at": access_exp["expires_at"],
            "access_token_is_expired": access_exp["is_expired"],
            "access_token_expires_in": access_exp["expires_in"],
            "refresh_token_expires_at": refresh_exp["expires_at"],
            "refresh_token_is_expired": refresh_exp["is_expired"],
            "refresh_token_expires_in": refresh_exp["expires_in"],
            "credentials_file": str(cls.CREDENTIALS_PATH),
        }

    @classmethod
    def trigger_login(cls, binary_path: Optional[str] = None) -> dict[str, Any]:
        """
        Pure Python PKCE OAuth URL generator for Claude. Zero claude.exe binary required.
        """
        verifier, challenge = cls.generate_pkce()
        state = secrets.token_urlsafe(32)

        cls._pending_oauth[state] = verifier
        cls._latest_verifier = verifier
        cls._latest_state = state

        params = {
            "code": "true",
            "client_id": cls.CLIENT_ID,
            "response_type": "code",
            "redirect_uri": cls.REDIRECT_URI,
            "scope": " ".join(cls.SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        auth_url = f"{cls.AUTH_URL}?{urllib.parse.urlencode(params)}"

        return {
            "ok": True,
            "provider": "claude",
            "redirect_url": auth_url,
            "requires_code": True,
            "state": state,
            "message": "Pure Python OAuth initiated. Opening Claude sign-in page in browser...",
        }

    @classmethod
    def exchange_code(cls, code: str, state: Optional[str] = None) -> dict[str, Any]:
        """
        Pure Python OAuth authorization code exchange against Anthropic endpoint.
        Zero claude.exe binary required.
        """
        code_raw = code.strip()
        if not code_raw:
            return {"ok": False, "error": "No authorization code provided"}

        if "#" in code_raw:
            code_val, state_val = code_raw.split("#", 1)
        else:
            code_val = code_raw
            state_val = state or cls._latest_state

        verifier = cls._pending_oauth.pop(state_val, None) or cls._latest_verifier
        if not verifier:
            return {
                "ok": False,
                "error": "No matching PKCE verifier found. Please click Authenticate Claude again.",
            }

        payload = {
            "grant_type": "authorization_code",
            "code": code_val,
            "state": state_val,
            "redirect_uri": cls.REDIRECT_URI,
            "client_id": cls.CLIENT_ID,
            "code_verifier": verifier,
        }

        try:
            req = urllib.request.Request(
                cls.TOKEN_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "claude-cli/2.1.281 (external, sdk-cli)",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                token_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            try:
                err_json = json.loads(err_body)
                msg = err_json.get("error_description") or err_json.get("error") or err_json.get("message") or err_body
            except Exception:
                msg = err_body
            return {"ok": False, "error": f"Token exchange failed ({e.code}): {msg}"}
        except Exception as e:
            return {"ok": False, "error": f"Network error during token exchange: {e}"}

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 28800)
        scope = token_data.get("scope", "")

        if not access_token:
            return {"ok": False, "error": f"Token response missing access_token: {token_data}"}

        # Try to fetch user profile via direct Anthropic API
        email = None
        org_uuid = None
        sub_type = "pro"
        try:
            prof_req = urllib.request.Request(
                cls.PROFILE_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Cache-Control": "no-cache",
                    "User-Agent": "claude-cli/2.1.281 (external, sdk-cli)",
                },
            )
            with urllib.request.urlopen(prof_req, timeout=10) as prof_resp:
                prof_data = json.loads(prof_resp.read().decode("utf-8"))
                email = prof_data.get("account", {}).get("email_address")
                org_uuid = prof_data.get("organization", {}).get("uuid")
                raw_sub = prof_data.get("organization", {}).get("organization_type")
                if raw_sub:
                    sub_type = str(raw_sub).lower()
                if email:
                    cls._cached_email = email
        except Exception:
            pass

        scopes_list = scope.split(" ") if isinstance(scope, str) else scope
        if not scopes_list:
            scopes_list = cls.SCOPES

        now_ms = int(time.time() * 1000)
        try:  # keep whatever else Claude Code stores in this file
            data = json.loads(cls.CREDENTIALS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.update({
            "claudeAiOauth": {
                "accessToken": access_token,
                "refreshToken": refresh_token or "",
                "expiresAt": now_ms + int(expires_in * 1000),
                "refreshTokenExpiresAt": now_ms + int(30 * 86400 * 1000),
                "scopes": scopes_list,
                "subscriptionType": sub_type,
                "rateLimitTier": "default_claude_ai",
            },
            "email": email or cls._cached_email,
            "organizationUuid": org_uuid,
        })

        _write_private(cls.CREDENTIALS_PATH, json.dumps(data, indent=2))
        return cls.get_credentials(reveal=False)

    @classmethod
    def save_tokens(
        cls,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        subscription: Optional[str] = None,
    ) -> dict[str, Any]:
        """Save manually provided tokens into ~/.claude/.credentials.json."""
        data = {}
        if cls.CREDENTIALS_PATH.is_file():
            try:
                data = json.loads(cls.CREDENTIALS_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        oauth = data.setdefault("claudeAiOauth", {})
        if access_token:
            oauth["accessToken"] = access_token.strip()
            oauth["expiresAt"] = int((time.time() + 8 * 3600) * 1000)
        if refresh_token:
            oauth["refreshToken"] = refresh_token.strip()
            oauth["refreshTokenExpiresAt"] = int((time.time() + 30 * 86400) * 1000)
        if subscription:
            oauth["subscriptionType"] = subscription.strip().lower()

        _write_private(cls.CREDENTIALS_PATH, json.dumps(data, indent=2))
        return cls.get_credentials(reveal=False)

    @classmethod
    def refresh(cls, binary_path: Optional[str] = None) -> dict[str, Any]:
        """
        Pure Python OAuth token refresh with Rotation (RTR).
        POSTs to https://platform.claude.com/v1/oauth/token. Zero claude.exe required.
        """
        if not cls.CREDENTIALS_PATH.is_file():
            return {"ok": False, "error": "No credentials file to refresh"}

        try:
            data = json.loads(cls.CREDENTIALS_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"Failed to read credentials: {e}"}

        oauth = data.get("claudeAiOauth", {})
        current_rf = oauth.get("refreshToken")
        if not current_rf:
            return {"ok": False, "error": "No refresh token available in ~/.claude/.credentials.json"}

        scopes = oauth.get("scopes") or cls.SCOPES
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": current_rf,
            "client_id": cls.CLIENT_ID,
            "scope": " ".join(scopes) if isinstance(scopes, list) else str(scopes),
        }

        try:
            req = urllib.request.Request(
                cls.TOKEN_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "claude-cli/2.1.281 (external, sdk-cli)",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                token_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            try:
                err_json = json.loads(err_body)
                msg = err_json.get("error_description") or err_json.get("error") or err_json.get("message") or err_body
            except Exception:
                msg = err_body
            return {"ok": False, "error": f"Token refresh failed ({e.code}): {msg}"}
        except Exception as e:
            return {"ok": False, "error": f"Network error during token refresh: {e}"}

        new_access = token_data.get("access_token")
        new_refresh = token_data.get("refresh_token") or current_rf
        expires_in = token_data.get("expires_in", 28800)

        if not new_access:
            return {"ok": False, "error": f"Token refresh response missing access_token: {token_data}"}

        now_ms = int(time.time() * 1000)
        oauth["accessToken"] = new_access
        oauth["refreshToken"] = new_refresh
        oauth["expiresAt"] = now_ms + int(expires_in * 1000)
        oauth["refreshTokenExpiresAt"] = now_ms + int(30 * 86400 * 1000)

        _write_private(cls.CREDENTIALS_PATH, json.dumps(data, indent=2))
        return cls.get_credentials(reveal=False)

    @classmethod
    def ensure_valid_token(cls) -> str:
        """
        Returns a valid, active access token. Refreshes automatically if expired or near expiry.
        Supports ANTHROPIC_API_KEY environment variable as fallback.
        """
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if api_key:
            return api_key

        if not cls.CREDENTIALS_PATH.is_file():
            raise RuntimeError("Not signed in to Claude. Please authenticate Claude on the dashboard.")

        data = json.loads(cls.CREDENTIALS_PATH.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth", {})
        access_tok = oauth.get("accessToken", "").strip()
        exp_ms = oauth.get("expiresAt") or 0
        now_ms = int(time.time() * 1000)

        if not access_tok or now_ms > (exp_ms - 300_000):
            res = cls.refresh()
            if not res.get("logged_in") and res.get("error"):
                raise RuntimeError(f"Claude token expired and refresh failed: {res.get('error')}. Please re-authenticate.")
            data = json.loads(cls.CREDENTIALS_PATH.read_text(encoding="utf-8"))
            access_tok = data.get("claudeAiOauth", {}).get("accessToken", "").strip()

        if not access_tok:
            raise RuntimeError("No active Claude access token found. Please authenticate Claude on the dashboard.")
        return access_tok


def _app_data_dir() -> Path:
    """Where VS Code-based apps such as Antigravity keep their user data."""
    if os.name == "nt":
        return Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")


class AntigravityAuth:
    """Handles credentials and Google OAuth for Google Antigravity (agy.exe)."""

    # OAuth client of the Antigravity app, for the in-app "Authenticate
    # Antigravity" flow. Read from the environment (.env), never committed.
    GOOGLE_CLIENT_ID = config.ANTIGRAVITY_OAUTH_CLIENT_ID
    GOOGLE_CLIENT_SECRET = config.ANTIGRAVITY_OAUTH_CLIENT_SECRET
    GOOGLE_SCOPES = (
        "https://www.googleapis.com/auth/cloud-platform "
        "https://www.googleapis.com/auth/userinfo.email "
        "https://www.googleapis.com/auth/userinfo.profile "
        "https://www.googleapis.com/auth/cclog "
        "https://www.googleapis.com/auth/experimentsandconfigs "
        "https://www.googleapis.com/auth/aicode"
    )
    GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

    _STATE_DB_PATHS = [
        _app_data_dir() / app / "User" / "globalStorage" / "state.vscdb"
        for app in ("Antigravity", "Antigravity IDE")
    ]

    _pending_oauth: dict[str, dict[str, Any]] = {}

    @classmethod
    def generate_pkce(cls) -> tuple[str, str]:
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").replace("=", "")
        return verifier, challenge

    @classmethod
    def find_state_db(cls) -> Optional[Path]:
        for p in cls._STATE_DB_PATHS:
            if p.is_file():
                return p
        return None

    @classmethod
    def get_credentials(cls, reveal: bool = False) -> dict[str, Any]:
        """Read tokens and account info from state.vscdb."""
        db_path = cls.find_state_db()
        if not db_path:
            return {
                "available": False,
                "provider": "antigravity",
                "label": "Antigravity",
                "logged_in": False,
                "error": "Antigravity state database (state.vscdb) not found.",
            }

        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            cur = conn.cursor()

            # 1. Basic auth status (name, email, apiKey)
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", ("antigravityAuthStatus",))
            row_auth = cur.fetchone()
            auth_status = json.loads(row_auth[0]) if row_auth and row_auth[0] else {}

            # 2. OAuth tokens (access & refresh tokens)
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", ("antigravityUnifiedStateSync.oauthToken",))
            row_tok = cur.fetchone()
            access_tok: Optional[str] = None
            refresh_tok: Optional[str] = None

            if row_tok and row_tok[0]:
                raw_b64 = row_tok[0]
                try:
                    b = base64.b64decode(raw_b64)
                    for s in re.findall(rb"[A-Za-z0-9+/=]{40,}", b):
                        try:
                            dec = base64.b64decode(s)
                            for m in re.findall(rb"1//[A-Za-z0-9_-]+", dec):
                                refresh_tok = m.decode("ascii")
                            for m in re.findall(rb"ya29\.[A-Za-z0-9_-]+", dec):
                                access_tok = m.decode("ascii")
                        except Exception:
                            pass
                except Exception:
                    pass

            # 3. User tier and status
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", ("antigravityUnifiedStateSync.userStatus",))
            row_status = cur.fetchone()
            tier = "Standard"
            if row_status and row_status[0]:
                try:
                    raw_user = base64.b64decode(row_status[0])
                    for s in re.findall(rb"[A-Za-z0-9+/=]{40,}", raw_user):
                        try:
                            dec = base64.b64decode(s)
                            if b"Google AI Pro" in dec or b"g1-pro-tier" in dec:
                                tier = "Google AI Pro"
                            elif b"Google AI Ultra" in dec:
                                tier = "Google AI Ultra"
                        except Exception:
                            pass
                except Exception:
                    pass

            conn.close()

            name = auth_status.get("name") or "User"
            email = auth_status.get("email") or ""
            api_key = auth_status.get("apiKey") or ""
            logged_in = bool(email or access_tok or api_key)

            return {
                "available": True,
                "provider": "antigravity",
                "label": "Antigravity",
                "logged_in": logged_in,
                "name": name,
                "email": email,
                "subscription": tier,
                "api_key": api_key if reveal else mask_token(api_key, 8, 4),
                "has_api_key": bool(api_key),
                "access_token": access_tok if reveal else mask_token(access_tok, 12, 6),
                "refresh_token": refresh_tok if reveal else mask_token(refresh_tok, 10, 6),
                "has_access_token": bool(access_tok),
                "has_refresh_token": bool(refresh_tok),
                "database_file": str(db_path),
            }

        except Exception as e:
            return {
                "available": True,
                "provider": "antigravity",
                "label": "Antigravity",
                "logged_in": False,
                "error": f"Error reading state.vscdb: {e}",
            }

    @classmethod
    def trigger_login(
        cls,
        binary_path: Optional[str] = None,
        redirect_uri: str = "http://localhost:8000/auth/antigravity/callback",
    ) -> dict[str, Any]:
        """
        Generate dynamic Google OAuth PKCE authorization URL for Google Antigravity.
        Opens directly in the user's browser with the Antigravity consent screen.
        Zero terminal popups.
        """
        if not cls.GOOGLE_CLIENT_ID:
            return {
                "ok": False,
                "provider": "antigravity",
                "error": "In-app Google login is not configured: set ANTIGRAVITY_OAUTH_CLIENT_ID and "
                         "ANTIGRAVITY_OAUTH_CLIENT_SECRET in .env, or sign in with agy.exe directly.",
            }

        # Clean expired pending OAuth requests (> 15 minutes)
        now = time.time()
        cls._pending_oauth = {k: v for k, v in cls._pending_oauth.items() if now - v.get("ts", 0) < 900}

        verifier, challenge = cls.generate_pkce()
        state = secrets.token_urlsafe(32)

        cls._pending_oauth[state] = {
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "ts": now,
        }

        params = {
            "access_type": "offline",
            "client_id": cls.GOOGLE_CLIENT_ID,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "consent",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": cls.GOOGLE_SCOPES,
            "state": state,
        }

        auth_url = f"{cls.GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
        return {
            "ok": True,
            "provider": "antigravity",
            "redirect_url": auth_url,
            "state": state,
            "api_studio_url": "https://aistudio.google.com/apikey",
            "message": "Dynamic Antigravity Google OAuth URL generated. Opening in browser...",
        }

    @classmethod
    def exchange_code(
        cls,
        code: str,
        state: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Exchange Google OAuth authorization code for access_token and refresh_token,
        fetch user profile, and persist to state.vscdb.
        """
        verifier = None
        expected_redirect_uri = redirect_uri or "http://localhost:8000/auth/antigravity/callback"

        # Only finish sign-ins started here: a code with an unknown state could
        # come from someone else's account (login CSRF).
        if not state or state not in cls._pending_oauth:
            return {"ok": False, "logged_in": False,
                    "error": "Unknown or expired sign-in. Click Authenticate Antigravity again."}
        saved = cls._pending_oauth.pop(state)
        verifier = saved.get("verifier")
        if saved.get("redirect_uri"):
            expected_redirect_uri = saved["redirect_uri"]

        token_payload = {
            "client_id": cls.GOOGLE_CLIENT_ID,
            "client_secret": cls.GOOGLE_CLIENT_SECRET,
            "code": code.strip(),
            "grant_type": "authorization_code",
            "redirect_uri": expected_redirect_uri,
        }
        if verifier:
            token_payload["code_verifier"] = verifier

        try:
            req = urllib.request.Request(
                cls.GOOGLE_TOKEN_URL,
                data=urllib.parse.urlencode(token_payload).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                token_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            return {"ok": False, "error": f"Google token exchange failed ({e.code}): {err_body}"}
        except Exception as e:
            return {"ok": False, "error": f"Failed to connect to Google OAuth: {e}"}

        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        expires_in = token_data.get("expires_in", 3600)

        if not access_token:
            return {"ok": False, "error": "No access_token returned by Google."}

        # Fetch user info from Google
        user_name = "User"
        user_email = ""
        user_picture = ""
        try:
            req_user = urllib.request.Request(
                cls.GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urllib.request.urlopen(req_user, timeout=8) as resp_user:
                user_info = json.loads(resp_user.read().decode("utf-8"))
                user_name = user_info.get("name") or "User"
                user_email = user_info.get("email") or ""
                user_picture = user_info.get("picture") or ""
        except Exception:
            pass

        # Persist into state.vscdb
        cls.save_tokens(
            access_token=access_token,
            refresh_token=refresh_token if refresh_token else None,
            user_name=user_name,
            user_email=user_email,
            picture_url=user_picture,
            expires_in=expires_in,
        )

        return cls.get_credentials(reveal=False)

    @classmethod
    def save_tokens(
        cls,
        api_key: Optional[str] = None,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        user_name: Optional[str] = None,
        user_email: Optional[str] = None,
        picture_url: Optional[str] = None,
        expires_in: int = 3600,
    ) -> dict[str, Any]:
        """Save API key, user info, and OAuth tokens into state.vscdb."""
        db_path = cls.find_state_db()
        if not db_path:
            return {"ok": False, "error": "state.vscdb not found"}
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()

            # 1. Update antigravityAuthStatus (name, email, apiKey)
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", ("antigravityAuthStatus",))
            row = cur.fetchone()
            auth_st = json.loads(row[0]) if row and row[0] else {}
            if api_key:
                auth_st["apiKey"] = api_key.strip()
            if user_name:
                auth_st["name"] = user_name.strip()
            if user_email:
                auth_st["email"] = user_email.strip()
            cur.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                ("antigravityAuthStatus", json.dumps(auth_st)),
            )

            # 2. Update profileUrl if available
            if picture_url:
                cur.execute(
                    "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                    ("antigravity.profileUrl", picture_url.strip()),
                )

            # 3. Update oauthToken protobuf if access_token or refresh_token is provided
            if access_token or refresh_token:
                existing_creds = cls.get_credentials(reveal=True)
                acc = access_token or existing_creds.get("access_token") or ""
                ref = refresh_token or existing_creds.get("refresh_token") or ""
                if acc:
                    packed_b64 = _pack_oauth_token_pb(acc, ref, expiry_secs=expires_in)
                    cur.execute(
                        "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                        ("antigravityUnifiedStateSync.oauthToken", packed_b64),
                    )

            conn.commit()
            conn.close()
            return cls.get_credentials(reveal=False)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @classmethod
    def refresh(cls, binary_path: Optional[str] = None) -> dict[str, Any]:
        """
        Refresh Antigravity access token using Google OAuth refresh_token or fallback to agy models.
        Zero terminal popups.
        """
        creds = cls.get_credentials(reveal=True)
        rf_token = creds.get("refresh_token")
        if rf_token and cls.GOOGLE_CLIENT_ID and cls.GOOGLE_CLIENT_SECRET:
            try:
                data = urllib.parse.urlencode({
                    "client_id": cls.GOOGLE_CLIENT_ID,
                    "client_secret": cls.GOOGLE_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": rf_token,
                }).encode("utf-8")
                req = urllib.request.Request(
                    cls.GOOGLE_TOKEN_URL,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                    new_acc = res.get("access_token")
                    if new_acc:
                        cls.save_tokens(access_token=new_acc, expires_in=res.get("expires_in", 3600))
                        return cls.get_credentials(reveal=False)
            except Exception:
                pass

        # Fallback to agy models execution silently
        target = binary_path
        if not target or not Path(target).is_file():
            from switchboard_ai.discovery import find_agy_binaries
            bins = find_agy_binaries()
            target = str(bins[0]) if bins else None

        if target and Path(target).is_file():
            try:
                extra_flags = {}
                if os.name == "nt":
                    extra_flags["creationflags"] = subprocess.CREATE_NO_WINDOW
                subprocess.run(
                    [target, "models"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=25,
                    check=False,
                    **extra_flags,
                )
            except Exception:
                pass

        return cls.get_credentials(reveal=False)


def get_all_auth_status(reveal: bool = False) -> dict[str, Any]:
    """Retrieve combined auth and token details for both providers."""
    return {
        "claude": ClaudeAuth.get_credentials(reveal=reveal),
        "antigravity": {
            **AntigravityAuth.get_credentials(reveal=reveal),
            # The in-app Google login needs an OAuth client in .env; otherwise
            # agy signs in on its own and the UI hides the button.
            "login_configured": bool(AntigravityAuth.GOOGLE_CLIENT_ID),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

