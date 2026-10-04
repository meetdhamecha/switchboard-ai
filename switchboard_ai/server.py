"""
switchboard_ai.server
─────────────────
One REST API over whichever CLI providers are installed.

    GET  /                       Web UI
    GET  /health                 Status of the server and each provider
    GET  /providers              Provider details
    GET  /models                 Every model (each has a unique "key")
    POST /route                  Explain which model "auto" would pick

    POST /chat                   Chat, full response
    POST /chat/stream            Chat, Server-Sent Events
    POST /agent/task             Agent task (file tools), full response
    POST /agent/task/stream      Agent task, Server-Sent Events
    GET  /sessions               Warm sessions per provider
    DELETE /sessions/{id}        Drop a session

    GET  /v1/models              OpenAI-compatible
    POST /v1/chat/completions    OpenAI-compatible (stream or not)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from switchboard_ai import __version__, config
from switchboard_ai.pacing import smooth_text_stream
from switchboard_ai.process import Msg
from switchboard_ai.providers import RoutingError, registry
from switchboard_ai.providers.base import EFFORT_LEVELS, Provider
from switchboard_ai.orchestrator import orchestrator
from switchboard_ai.router import MODES, is_auto, mode_of, router

STATIC_DIR = Path(__file__).parent / "static"


# ============================================================
# LIFESPAN
# ============================================================

def _banner() -> None:
    host = "localhost" if config.API_HOST in ("0.0.0.0", "127.0.0.1") else config.API_HOST
    dp, dm = registry.default()
    print("\n" + "=" * 64)
    print(f"  SWITCHBOARD AI v{__version__}")
    print("=" * 64)
    for p in registry.providers.values():
        state = "READY" if p.available else ("disabled" if not p.enabled else "not found")
        print(f"  {p.label:<13}: {state:<10} {p.binary or ''}")
    print(f"  Default      : {f'{dp.id}:{dm}' if dp else '— no provider available —'}")
    print(f"  Auth         : {'API key required' if config.API_KEY else 'OPEN (no API_KEY set)'}")
    if "*" in config.CORS_ORIGINS and not config.API_KEY:
        print("  WARNING      : CORS_ORIGINS=* with no API_KEY lets any website use this server")
    print(f"  Agent shell  : {'ENABLED' if config.AGENT_ALLOW_SHELL else 'off'}")
    print(f"  UI           : http://{host}:{config.API_PORT}/")
    print(f"  Docs         : http://{host}:{config.API_PORT}/docs")
    if any(p.enabled and not p.available for p in registry.providers.values()):
        print("  Missing a provider? Run `switchboard-ai setup` to install it.")
    print("=" * 64 + "\n")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _banner()
    dp, dm = registry.default()
    listed: dict[str, list[str]] = {}
    for key in config.WARM_MODELS if config.WARMUP_ON_STARTUP else []:
        try:
            p, m = registry.resolve(key, None)
            listed.setdefault(p.id, []).append(m)
        except RoutingError as e:
            print(f"  WARM_MODELS: skipping {key!r}: {e.detail}")

    async def start(p: Provider) -> None:
        warm: list[str] = []
        if config.WARMUP_ON_STARTUP:
            warm = listed.get(p.id) or ([dm] if p is dp else [m["id"] for m in p.models()[:1]])
        try:
            await p.startup(warm)
        except Exception as e:
            print(f"  [{p.id}] startup warning: {e}")

    # In the background, so the server accepts requests immediately.
    boot = [asyncio.create_task(start(p)) for p in registry.available]
    yield
    for t in boot:
        t.cancel()
    for p in registry.available:
        await p.shutdown()


app = FastAPI(
    title="Switchboard AI",
    description=(
        "One REST API for the Claude Code (claude.exe) and Antigravity (agy.exe) "
        "CLIs installed on this machine — works with either or both."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials="*" not in config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _allowed_hosts() -> list[str]:
    if config.TRUSTED_HOSTS:
        return config.TRUSTED_HOSTS
    return ["localhost", "127.0.0.1", "[::1]"] if config.API_HOST in _LOOPBACK else []


@app.middleware("http")
async def _block_other_sites(request: Request, call_next):
    """Stop other websites from using this server through the user's browser:
    unknown Host names (DNS rebinding) and requests from pages on another
    origin (CSRF) are refused unless CORS_ORIGINS allows that origin."""
    host = (request.headers.get("host") or "").lower()
    name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    hosts = _allowed_hosts()
    if hosts and name not in hosts:
        return JSONResponse(status_code=403, content={"detail": f"Host not allowed: {name}. Set TRUSTED_HOSTS in .env."})
    origin = request.headers.get("origin")
    if (origin and "*" not in config.CORS_ORIGINS and origin not in config.CORS_ORIGINS
            and origin.lower() != f"{request.url.scheme}://{host}"):
        return JSONResponse(status_code=403, content={
            "detail": f"Requests from {origin} are blocked. Add it to CORS_ORIGINS in .env to allow it."})
    return await call_next(request)


@app.exception_handler(RoutingError)
async def _routing_error(_: Request, exc: RoutingError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


# ============================================================
# AUTH
# ============================================================

def _key_ok(key: Optional[str]) -> bool:
    return bool(key) and hmac.compare_digest(key.encode(), config.API_KEY.encode())


async def require_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> None:
    if not config.API_KEY:
        return
    token = x_api_key or (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing API key (Authorization: Bearer <key> or X-API-Key)")
    if not _key_ok(token):
        raise HTTPException(403, "Invalid API key")


class KeyBody(BaseModel):
    key: str


class ProviderTargetBody(BaseModel):
    provider: str = Field("all", description="claude | antigravity | all")


@app.get("/auth/status", tags=["Auth"])
async def auth_status():
    return {"auth_required": bool(config.API_KEY)}


@app.post("/auth/verify", tags=["Auth"])
async def auth_verify(body: KeyBody):
    if not config.API_KEY or _key_ok(body.key):
        return {"valid": True}
    raise HTTPException(401, "Invalid API key")


@app.get("/auth/accounts", tags=["Auth"], dependencies=[Depends(require_key)])
def auth_accounts():
    from switchboard_ai.auth import get_all_auth_status
    return get_all_auth_status(reveal=False)


@app.get("/auth/tokens", tags=["Auth"], dependencies=[Depends(require_key)])
def auth_tokens(reveal: bool = False):
    from switchboard_ai.auth import get_all_auth_status
    return get_all_auth_status(reveal=reveal)


def _render_auth_success_page(name: str, email: str) -> str:
    escaped_name = name.replace("<", "&lt;").replace(">", "&gt;")
    escaped_email = email.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Google Antigravity - Authentication Successful</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      background: #0f1015;
      color: #e4e4e7;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .auth-card {{
      background: #181920;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 20px;
      padding: 44px 36px;
      max-width: 460px;
      width: 100%;
      text-align: center;
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255, 255, 255, 0.05);
      animation: pop 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    }}
    @keyframes pop {{
      from {{ opacity: 0; transform: scale(0.96) translateY(12px); }}
      to {{ opacity: 1; transform: scale(1) translateY(0); }}
    }}
    .logo-badge {{
      width: 68px;
      height: 68px;
      margin: 0 auto 24px;
      border-radius: 18px;
      background: linear-gradient(135deg, rgba(66, 133, 244, 0.2), rgba(52, 168, 83, 0.2));
      border: 1px solid rgba(66, 133, 244, 0.3);
      display: grid;
      place-items: center;
      box-shadow: 0 8px 24px -6px rgba(66, 133, 244, 0.4);
    }}
    h1 {{
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #ffffff;
      margin-bottom: 10px;
    }}
    .subtext {{
      color: #94a3b8;
      font-size: 14.5px;
      line-height: 1.5;
      margin-bottom: 24px;
    }}
    .user-pill {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 999px;
      padding: 7px 16px;
      margin-bottom: 26px;
      font-size: 13.5px;
    }}
    .dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #22c55e;
      box-shadow: 0 0 10px #22c55e;
    }}
    .close-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      width: 100%;
      padding: 12px 20px;
      border-radius: 12px;
      background: #4f8cff;
      color: #fff;
      border: none;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s, transform 0.1s;
    }}
    .close-btn:hover {{ background: #3b76e1; }}
    .close-btn:active {{ transform: scale(0.98); }}
    .countdown {{
      margin-top: 14px;
      font-size: 12px;
      color: #64748b;
    }}
  </style>
</head>
<body>
  <div class="auth-card">
    <div class="logo-badge">
      <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#4285F4" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
        <polyline points="22 4 12 14.01 9 11.01" stroke="#22c55e"/>
      </svg>
    </div>
    <h1>You have successfully authenticated</h1>
    <p class="subtext">Google Antigravity is now connected to Switchboard AI with live OAuth tokens.</p>
    <div class="user-pill">
      <span class="dot"></span>
      <b>{escaped_name}</b>
      <span style="color:#64748b">&bull;</span>
      <span style="color:#94a3b8">{escaped_email}</span>
    </div>
    <button class="close-btn" onclick="window.close()">Close Window</button>
    <div class="countdown">This window will close automatically...</div>
  </div>
  <script>
    try {{
      if (window.opener) {{
        window.opener.postMessage({{
          type: 'antigravity_auth_success',
          provider: 'antigravity',
          name: '{escaped_name}',
          email: '{escaped_email}'
        }}, '*');
      }}
    }} catch(e) {{}}
    setTimeout(function() {{
      try {{ window.close(); }} catch(e) {{}}
    }}, 2000);
  </script>
</body>
</html>"""


def _render_auth_error_page(title: str, detail: str) -> str:
    escaped_title = title.replace("<", "&lt;").replace(">", "&gt;")
    escaped_detail = detail.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Google Antigravity - Authentication Error</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f1015;
      color: #e4e4e7;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .card {{
      background: #181920;
      border: 1px solid rgba(239, 68, 68, 0.25);
      border-radius: 16px;
      padding: 36px;
      max-width: 440px;
      text-align: center;
    }}
    h1 {{ color: #ef4444; font-size: 20px; margin-bottom: 12px; }}
    p {{ color: #94a3b8; font-size: 14px; line-height: 1.5; margin-bottom: 20px; word-break: break-all; }}
    button {{
      padding: 10px 18px; border-radius: 8px; background: rgba(255,255,255,0.08);
      color: #fff; border: 1px solid rgba(255,255,255,0.15); cursor: pointer;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{escaped_title}</h1>
    <p>{escaped_detail}</p>
    <button onclick="window.close()">Close Window</button>
  </div>
</body>
</html>"""


@app.post("/auth/login", tags=["Auth"], dependencies=[Depends(require_key)])
def auth_login(body: ProviderTargetBody, request: Request):
    from switchboard_ai.auth import AntigravityAuth, ClaudeAuth
    pid = body.provider.lower().strip()
    results = {}
    if pid in ("claude", "all"):
        cp = registry.get("claude")
        bin_path = cp.binary if cp else None
        results["claude"] = ClaudeAuth.trigger_login(bin_path)
    if pid in ("antigravity", "all"):
        ap = registry.get("antigravity")
        bin_path = ap.binary if ap else None
        base_url = str(request.base_url).rstrip("/")
        redirect_uri = f"{base_url}/auth/antigravity/callback"
        results["antigravity"] = AntigravityAuth.trigger_login(bin_path, redirect_uri=redirect_uri)
    if not results:
        raise HTTPException(400, f"Unknown provider: {body.provider}")
    return results


def _agy_provider() -> Provider:
    p = registry.get("antigravity")
    if not p or not p.available:
        raise HTTPException(503, "The Antigravity CLI (agy) is not installed. Run `switchboard-ai setup`.")
    return p


@app.get("/auth/antigravity/cli", tags=["Auth"], dependencies=[Depends(require_key)])
async def agy_cli_status(refresh: bool = False):
    """Whether agy itself is signed in, and the state of a sign-in started here."""
    from switchboard_ai.agy_auth import agy_auth
    p = registry.get("antigravity")
    if not p or not p.available:
        return {"available": False}
    return await agy_auth.status(p, refresh)


@app.post("/auth/antigravity/cli/login", tags=["Auth"], dependencies=[Depends(require_key)])
async def agy_cli_login():
    """Open agy's sign-in and return the Google sign-in link for the browser."""
    from switchboard_ai.agy_auth import agy_auth
    return await agy_auth.start_login(_agy_provider())


class AgyCodeBody(BaseModel):
    code: str = Field(..., max_length=512, description="Authorization code Google showed after sign-in")


@app.post("/auth/antigravity/cli/login/code", tags=["Auth"], dependencies=[Depends(require_key)])
def agy_cli_login_code(body: AgyCodeBody):
    """Pass the authorization code from Google's page to the sign-in in progress."""
    from switchboard_ai.agy_auth import agy_auth
    return agy_auth.submit_code(body.code)


@app.post("/auth/antigravity/cli/login/cancel", tags=["Auth"], dependencies=[Depends(require_key)])
def agy_cli_login_cancel():
    from switchboard_ai.agy_auth import agy_auth
    return agy_auth.cancel_login()


@app.post("/auth/antigravity/cli/logout", tags=["Auth"], dependencies=[Depends(require_key)])
async def agy_cli_logout():
    """Run agy's /logout and stop the agy processes that used the old account."""
    from switchboard_ai.agy_auth import agy_auth
    return await agy_auth.logout(_agy_provider())


class AgySetupBody(BaseModel):
    accept_terms: bool = Field(False, description="The user agreed to the Antigravity CLI terms in the UI")
    share_data: bool = Field(False, description="Let Google collect and use Interactions data")


@app.post("/auth/antigravity/cli/setup", tags=["Auth"], dependencies=[Depends(require_key)])
async def agy_cli_setup(body: AgySetupBody):
    """Finish agy's one-time setup with the choices the user made in the UI."""
    from switchboard_ai.agy_auth import agy_auth
    if not body.accept_terms:
        raise HTTPException(400, "The Antigravity CLI terms must be accepted to finish its setup.")
    return await agy_auth.setup(_agy_provider(), body.share_data)


@app.get("/auth/antigravity/callback", response_class=HTMLResponse, tags=["Auth"])
def auth_antigravity_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    request: Request = None,
):
    from switchboard_ai.auth import AntigravityAuth

    if error:
        return HTMLResponse(
            content=_render_auth_error_page("Antigravity Authentication Cancelled", f"Google OAuth returned error: {error}"),
            status_code=400,
        )
    if not code:
        return HTMLResponse(
            content=_render_auth_error_page("Invalid Callback", "No authorization code was provided in the callback."),
            status_code=400,
        )

    base_url = str(request.base_url).rstrip("/") if request else "http://localhost:8000"
    redirect_uri = f"{base_url}/auth/antigravity/callback"
    result = AntigravityAuth.exchange_code(code=code, state=state, redirect_uri=redirect_uri)

    if not result.get("logged_in") and result.get("error"):
        return HTMLResponse(
            content=_render_auth_error_page("Authentication Failed", str(result["error"])),
            status_code=400,
        )

    name = result.get("name") or "User"
    email = result.get("email") or ""
    return HTMLResponse(content=_render_auth_success_page(name=name, email=email))


@app.post("/auth/refresh", tags=["Auth"], dependencies=[Depends(require_key)])
def auth_refresh(body: ProviderTargetBody):
    from switchboard_ai.auth import AntigravityAuth, ClaudeAuth
    pid = body.provider.lower().strip()
    results = {}
    if pid in ("claude", "all"):
        cp = registry.get("claude")
        bin_path = cp.binary if cp else None
        results["claude"] = ClaudeAuth.refresh(bin_path)
    if pid in ("antigravity", "all"):
        ap = registry.get("antigravity")
        bin_path = ap.binary if ap else None
        results["antigravity"] = AntigravityAuth.refresh(bin_path)
    if not results:
        raise HTTPException(400, f"Unknown provider: {body.provider}")
    return results


class SaveTokenBody(BaseModel):
    provider: str = Field(..., description="claude | antigravity")
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    api_key: Optional[str] = None
    subscription: Optional[str] = None


class ExchangeCodeBody(BaseModel):
    provider: str = Field(..., description="claude | antigravity")
    code: str = Field(..., description="OAuth authorization code")
    state: Optional[str] = Field(None, description="OAuth state if antigravity")


@app.post("/auth/exchange", tags=["Auth"], dependencies=[Depends(require_key)])
def auth_exchange(body: ExchangeCodeBody, request: Request):
    from switchboard_ai.auth import AntigravityAuth, ClaudeAuth
    pid = body.provider.lower().strip()
    if pid == "claude":
        res = ClaudeAuth.exchange_code(body.code, state=body.state)
        if not res.get("logged_in") and res.get("error"):
            raise HTTPException(400, res["error"])
        return res
    if pid == "antigravity":
        base_url = str(request.base_url).rstrip("/")
        redirect_uri = f"{base_url}/auth/antigravity/callback"
        res = AntigravityAuth.exchange_code(code=body.code, state=body.state, redirect_uri=redirect_uri)
        if not res.get("logged_in") and res.get("error"):
            raise HTTPException(400, res["error"])
        return res
    raise HTTPException(400, f"Code exchange not supported for provider: {body.provider}")


@app.post("/auth/save", tags=["Auth"], dependencies=[Depends(require_key)])
def auth_save(body: SaveTokenBody):
    from switchboard_ai.auth import AntigravityAuth, ClaudeAuth
    pid = body.provider.lower().strip()
    if pid == "claude":
        return ClaudeAuth.save_tokens(
            access_token=body.access_token,
            refresh_token=body.refresh_token,
            subscription=body.subscription,
        )
    if pid == "antigravity":
        return AntigravityAuth.save_tokens(
            api_key=body.api_key,
            access_token=body.access_token,
            refresh_token=body.refresh_token,
        )
    raise HTTPException(400, f"Unknown provider: {body.provider}")




# ============================================================
# SCHEMAS
# ============================================================

class Message(BaseModel):
    role: str = Field(..., description="user | assistant | system")
    content: str


_EFFORT_HELP = (
    "Thinking effort: low | medium | high | xhigh | max. Claude uses it as given; "
    "Gemini and GPT-OSS switch to the matching -low / -medium / -high model. A level "
    "the model lacks uses the nearest one. GET /models lists each model's levels."
)


def _check_effort(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip().lower() or None
    if v and v not in EFFORT_LEVELS:
        raise ValueError(f"effort must be one of: {', '.join(EFFORT_LEVELS)}")
    return v


class ChatRequest(BaseModel):
    messages: list[Message] = Field(..., min_length=1)
    model: Optional[str] = Field(
        None, description="Model id, 'provider:id', or 'auto' (smart routing). Empty = default."
    )
    provider: Optional[str] = Field(None, description="claude | antigravity (optional)")
    session_id: Optional[str] = Field(
        None, description="Keep a warm conversation. After the first turn you may send only the new message."
    )
    effort: Optional[str] = Field(None, description=_EFFORT_HELP)

    validate_effort = field_validator("effort")(_check_effort)

    model_config = {
        "json_schema_extra": {
            "example": {
                "model": "claude-sonnet-5",
                "messages": [{"role": "user", "content": "Hello!"}],
                "session_id": "my-chat-1",
                "effort": "medium",
            }
        }
    }


class AgentRequest(BaseModel):
    task: str
    model: Optional[str] = None
    provider: Optional[str] = None
    working_dir: Optional[str] = Field(None, description="Directory the agent may read and edit")
    tools: Optional[list[str]] = Field(None, description="Claude only: subset of the configured agent tools")
    resume: Optional[str] = Field(None, description="native_session_id from a previous task, to continue it")
    effort: Optional[str] = Field(None, description=_EFFORT_HELP)

    validate_effort = field_validator("effort")(_check_effort)

    model_config = {
        "json_schema_extra": {
            "example": {
                "task": "Read the Python files here and write a short SUMMARY.md",
                "model": "claude-sonnet-5",
                "working_dir": "C:\\Projects\\myapp",
            }
        }
    }


# ============================================================
# HELPERS
# ============================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _msgs(messages: list[Message]) -> list[Msg]:
    return [Msg(m.role, m.content) for m in messages]


def _sse(obj: Union[dict, str]) -> str:
    return f"data: {obj if isinstance(obj, str) else json.dumps(obj)}\n\n"


_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


def _paced(stream: AsyncGenerator[dict, None]) -> AsyncGenerator[dict, None]:
    return smooth_text_stream(stream) if config.SMOOTH_STREAM else stream


async def _collect(stream: AsyncGenerator[dict, None]) -> dict:
    text: list[str] = []
    tools: dict[str, dict] = {}
    notices: list[str] = []
    errors: list[str] = []
    codes: list[str] = []
    final: dict = {}
    route: Optional[dict] = None
    async for ev in stream:
        t = ev["type"]
        if t == "route":
            route = ev
        elif t == "text":
            text.append(ev["content"])
        elif t == "tool_use":
            tools.setdefault(ev["id"], {"name": ev["name"], "detail": "", "ok": None})
            if ev.get("detail"):
                tools[ev["id"]]["detail"] = ev["detail"]
        elif t == "tool_result" and ev["id"] in tools:
            tools[ev["id"]]["ok"] = ev["ok"]
        elif t == "notice":
            notices.append(ev["content"])
        elif t == "error":
            errors.append(ev["content"])
            if ev.get("code"):
                codes.append(ev["code"])
        elif t == "result":
            final = ev
    return {
        "content": "".join(text).strip() or final.get("content", ""),
        "tools": list(tools.values()),
        "notices": notices,
        "errors": errors,
        "final": final,
        "route": route,
        "codes": codes,
    }


def _raise_failure(out: dict) -> None:
    """No answer at all: 429 when overloaded / rate limited, else 502."""
    if out["errors"] and not out["content"]:
        if {"busy", "rate_limited"} & set(out["codes"]):
            raise HTTPException(429, out["errors"][-1], headers={"Retry-After": "10"})
        raise HTTPException(502, out["errors"][-1])


async def _chat_plan(
    model: Optional[str],
    provider: Optional[str],
    messages: list[Msg],
    session_id: Optional[str],
    effort: Optional[str],
) -> tuple[str, str, Optional[str], AsyncGenerator[dict, None]]:
    """(provider id, model, effort, event stream) for a named model or an
    auto-* model. Model and effort are what will actually run (a Gemini
    effort picks that variant of the model)."""
    mode = mode_of(model)
    if mode:
        name = next(k for k, v in MODES.items() if v == mode)
        return "auto", name, effort, orchestrator.stream(mode, messages, session_id, effort)
    p, m = registry.resolve(model, provider)
    m, e = p.resolve_effort(m, effort)
    return p.id, m, e or None, p.chat(messages, m, session_id, e)


def _routed(out: dict, pid: str, model: str, effort: Optional[str]) -> tuple[str, str, Optional[str]]:
    """The provider, model and effort that actually answered."""
    r = out.get("route")
    return (r["provider"], r["model"], r.get("effort")) if r else (pid, model, effort)


def _working_dir(requested: Optional[str]) -> str:
    wd = Path(requested or config.AGENT_WORKING_DIR).expanduser()
    try:
        wd = wd.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(400, f"working_dir does not exist: {requested}")
    if not wd.is_dir():
        raise HTTPException(400, f"working_dir is not a directory: {wd}")
    if config.AGENT_ALLOWED_ROOTS:
        roots = [Path(r).expanduser().resolve() for r in config.AGENT_ALLOWED_ROOTS]
        if not any(wd == r or r in wd.parents for r in roots):
            raise HTTPException(403, f"working_dir must be inside: {', '.join(map(str, roots))}")
    return str(wd)


# ============================================================
# SYSTEM
# ============================================================

_INFO_TTL = 5.0
_info_cache: tuple[float, dict] = (0.0, {})


async def _provider_info() -> dict:
    """p.info() reads credential files / SQLite: run it in a thread, cache 5 s."""
    global _info_cache
    at, data = _info_cache
    if time.monotonic() - at > _INFO_TTL:
        data = await run_in_threadpool(lambda: {p.id: p.info() for p in registry.providers.values()})
        _info_cache = (time.monotonic(), data)
    return data


@app.get("/health", tags=["System"])
async def health():
    dp, dm = registry.default()
    return {
        "status": "ok" if registry.available else "no_providers",
        "version": __version__,
        "providers": await _provider_info(),
        "default_model": f"{dp.id}:{dm}" if dp else None,
        "auth_required": bool(config.API_KEY),
        "agent": {
            "shell_enabled": config.AGENT_ALLOW_SHELL,
            "working_dir": config.AGENT_WORKING_DIR,
            "tools": {p.id: p.agent_tools() for p in registry.available},
        },
        "smooth_stream": config.SMOOTH_STREAM,
        "auto_router": router.info(),
        "timestamp": _now(),
    }


@app.get("/providers", tags=["System"])
async def providers():
    return list((await _provider_info()).values())


@app.get("/models", tags=["System"])
async def models(available_only: bool = False):
    ms = registry.models()
    return [m for m in ms if m["available"]] if available_only else ms


class RouteRequest(BaseModel):
    messages: list[Message] = Field(..., min_length=1)
    session_id: Optional[str] = None
    agent: bool = False


@app.post("/route", tags=["System"], dependencies=[Depends(require_key)])
async def explain_route(req: RouteRequest):
    """What model 'auto' would pick for these messages, without running it."""
    route = await router.route(_msgs(req.messages), None, req.agent)
    if not route.candidates:
        raise HTTPException(503, registry._none_available())
    return route.describe(0)


# ============================================================
# CHAT
# ============================================================

@app.post("/chat", tags=["Chat"], dependencies=[Depends(require_key)])
async def chat(req: ChatRequest):
    t0 = time.monotonic()
    pid, model, effort, events = await _chat_plan(
        req.model, req.provider, _msgs(req.messages), req.session_id, req.effort)
    out = await _collect(events)
    _raise_failure(out)
    final = out["final"]
    pid, model, effort = _routed(out, pid, model, effort)
    return {
        "id": f"msg_{uuid.uuid4().hex[:16]}",
        "provider": pid,
        "model": model,
        "effort": effort,
        "routing": out["route"],
        "content": out["content"],
        "session_id": req.session_id,
        "native_session_id": final.get("native_session_id"),
        "usage": final.get("usage", {}),
        "cost_usd": final.get("cost_usd", 0),
        "stop_reason": final.get("stop_reason", "unknown"),
        "tools": out["tools"],
        "notices": out["notices"],
        "latency_ms": round((time.monotonic() - t0) * 1000),
        "created_at": _now(),
    }


@app.post("/chat/stream", tags=["Chat"], dependencies=[Depends(require_key)])
async def chat_stream(req: ChatRequest):
    pid, model, effort, events = await _chat_plan(
        req.model, req.provider, _msgs(req.messages), req.session_id, req.effort)

    async def gen():
        yield _sse({"type": "session", "session_id": req.session_id, "provider": pid, "model": model,
                    "effort": effort})
        async for ev in _paced(events):
            yield _sse(ev)
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ============================================================
# AGENT
# ============================================================

async def _agent_setup(req: AgentRequest) -> tuple[Provider, str, str, Optional[str], Optional[dict]]:
    """(provider, model, working_dir, effort, route info)"""
    wd = _working_dir(req.working_dir)
    if is_auto(req.model):
        route = await router.route([Msg("user", req.task)], agent=True)
        if not route.candidates:
            raise HTTPException(503, registry._none_available())
        p, model, effort = route.candidates[0]
        model, effort = p.resolve_effort(model, req.effort or effort)
        return p, model, wd, effort or None, route.describe(0)
    p, model = registry.resolve(req.model, req.provider)
    model, effort = p.resolve_effort(model, req.effort)
    return p, model, wd, effort or None, None


@app.post("/agent/task", tags=["Agent"], dependencies=[Depends(require_key)])
async def agent_task(req: AgentRequest):
    p, model, wd, effort, route = await _agent_setup(req)
    t0 = time.monotonic()
    out = await _collect(p.agent(req.task, model, wd, req.tools, req.resume, effort))
    _raise_failure(out)
    final = out["final"]
    return {
        "id": f"task_{uuid.uuid4().hex[:16]}",
        "provider": p.id,
        "model": model,
        "effort": effort,
        "routing": route,
        "content": out["content"],
        "working_dir": wd,
        "native_session_id": final.get("native_session_id"),
        "tools": out["tools"],
        "notices": out["notices"],
        "usage": final.get("usage", {}),
        "cost_usd": final.get("cost_usd", 0),
        "latency_ms": round((time.monotonic() - t0) * 1000),
        "created_at": _now(),
    }


@app.post("/agent/task/stream", tags=["Agent"], dependencies=[Depends(require_key)])
async def agent_task_stream(req: AgentRequest):
    p, model, wd, effort, route = await _agent_setup(req)

    async def gen():
        yield _sse({
            "type": "session", "provider": p.id, "model": model, "effort": effort,
            "working_dir": wd, "tools": p.agent_tools(),
        })
        if route:
            yield _sse(route)
        async for ev in _paced(p.agent(req.task, model, wd, req.tools, req.resume, effort)):
            yield _sse(ev)
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ============================================================
# SESSIONS
# ============================================================

@app.get("/sessions", tags=["Sessions"], dependencies=[Depends(require_key)])
async def sessions():
    out = {p.id: p.pool.stats() for p in registry.available if p.pool}
    for p in registry.available:
        if not p.pool and getattr(p, "slots", None):
            out[p.id] = {"concurrency": p.slots.stats(), "stateless": True}
    return out


@app.delete("/sessions/{session_id}", tags=["Sessions"], dependencies=[Depends(require_key)])
async def drop_session(session_id: str):
    dropped = False
    for p in registry.available:
        if p.pool and await p.pool.drop(session_id):
            dropped = True
    return {"session_id": session_id, "dropped": dropped}


# ============================================================
# OPENAI-COMPATIBLE
# ============================================================
# Lets any OpenAI SDK / tool talk to this server:
#   OpenAI(base_url="http://localhost:8000/v1", api_key="<API_KEY or anything>")
# Pass header "X-Session-Id" (or body "session_id") to keep a warm conversation.

class OAIMessage(BaseModel):
    role: str
    content: Union[str, list, None] = ""


class OAIRequest(BaseModel):
    model: Optional[str] = None
    messages: list[OAIMessage] = Field(..., min_length=1)
    stream: bool = False
    session_id: Optional[str] = None
    reasoning_effort: Optional[str] = Field(None, description=_EFFORT_HELP)
    effort: Optional[str] = Field(None, description="Same as reasoning_effort")

    model_config = {"extra": "allow"}

    @field_validator("reasoning_effort", "effort")
    @classmethod
    def validate_effort(cls, v: Optional[str]) -> Optional[str]:
        # OpenAI clients may send these; the lowest level here is "low".
        if (v or "").strip().lower() in ("minimal", "none"):
            return "low"
        return _check_effort(v)


def _oai_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "input_text")
        )
    return ""


@app.get("/v1/models", tags=["OpenAI compatible"], dependencies=[Depends(require_key)])
async def oai_models():
    return {
        "object": "list",
        "data": [
            {"id": m["key"], "object": "model", "created": 0, "owned_by": m["provider"]}
            for m in registry.models() if m["available"]
        ],
    }


@app.post("/v1/chat/completions", tags=["OpenAI compatible"], dependencies=[Depends(require_key)])
async def oai_chat(req: OAIRequest, x_session_id: Optional[str] = Header(None)):
    msgs = [
        Msg("system" if m.role == "developer" else m.role, _oai_text(m.content))
        for m in req.messages if m.role in ("system", "developer", "user", "assistant")
    ]
    sid = req.session_id or x_session_id
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    pid, model, effort, events = await _chat_plan(req.model, None, msgs, sid, req.reasoning_effort or req.effort)
    name = "auto" if pid == "auto" else f"{pid}:{model}"

    if not req.stream:
        out = await _collect(events)
        _raise_failure(out)
        rpid, rmodel, _ = _routed(out, pid, model, effort)
        name = f"{rpid}:{rmodel}"
        u = out["final"].get("usage", {})
        pt, ct = u.get("input_tokens") or 0, u.get("output_tokens") or 0
        return {
            "id": cid, "object": "chat.completion", "created": created, "model": name,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": out["content"]},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
        }

    def chunk(delta: dict, finish: Optional[str] = None) -> str:
        return _sse({
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": name,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        })

    async def gen():
        yield chunk({"role": "assistant", "content": ""})
        async for ev in _paced(events):
            if ev["type"] == "text":
                yield chunk({"content": ev["content"]})
            elif ev["type"] == "error":
                yield _sse({"error": {"message": ev["content"], "type": "provider_error"}})
        yield chunk({}, "stop")
        yield _sse("[DONE]")

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


# ============================================================
# UI
# ============================================================

@app.get("/", include_in_schema=False)
async def ui():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        return RedirectResponse("/docs")
    return FileResponse(str(index), media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/chat-ui", include_in_schema=False)
async def ui_legacy():
    return RedirectResponse("/")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
