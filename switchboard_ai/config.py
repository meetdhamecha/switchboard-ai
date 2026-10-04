"""
switchboard_ai.config
─────────────────
All settings, loaded from `.env` (package dir first, then its parent).

Every value has a safe default, so the server runs with no .env at all.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PKG_DIR = Path(__file__).resolve().parent

# .env in the current directory (pip installs), else the repository root /
# package folder (running from a clone).
for _env_path in (Path.cwd() / ".env", PKG_DIR.parent / ".env", PKG_DIR / ".env"):
    if _env_path.exists():
        load_dotenv(_env_path)
        break


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _csv(name: str, default: str) -> list[str]:
    return [p.strip() for p in os.getenv(name, default).split(",") if p.strip()]


# ============================================================
# BINARIES
# ============================================================
# "auto" (or empty) = search the usual install locations. See discovery.py.

CLAUDE_BINARY_PATH: str = os.getenv("CLAUDE_BINARY_PATH", "auto").strip()
AGY_BINARY_PATH: str = os.getenv("AGY_BINARY_PATH", "auto").strip()

# Turn a provider off even when its binary is installed.
ENABLE_CLAUDE: bool = _bool("ENABLE_CLAUDE", "1")
ENABLE_ANTIGRAVITY: bool = _bool("ENABLE_ANTIGRAVITY", "1")


# ============================================================
# CLAUDE API  (providers/claude_api.py)
# ============================================================
# Official Anthropic API with a Claude Console key: pay-per-token, handles
# many parallel requests, and is the permitted way to serve other users.
# Deliberately NOT read from ANTHROPIC_API_KEY: claude.exe would inherit that
# name and silently switch your subscription CLI to API billing.
CLAUDE_API_KEY: str = os.getenv("SWITCHBOARD_ANTHROPIC_API_KEY", "").strip()
# Parallel API requests in flight (the API's own limits depend on your tier).
CLAUDE_API_MAX_CONCURRENT: int = int(os.getenv("CLAUDE_API_MAX_CONCURRENT", "64"))
# When a key is set, send bare Claude model ids (and "auto") to the API
# instead of claude.exe. Name "claude:<model>" to force the CLI.
PREFER_CLAUDE_API: bool = _bool("PREFER_CLAUDE_API", "1")
# Server-side web search / fetch on API chat turns (billed per search).
CLAUDE_API_WEB_TOOLS: bool = _bool("CLAUDE_API_WEB_TOOLS", "1")

# OAuth client for the in-app "Authenticate Antigravity" Google login and
# token refresh. Keep these in .env (gitignored). Unset = sign in via agy.exe.
ANTIGRAVITY_OAUTH_CLIENT_ID: str = os.getenv("ANTIGRAVITY_OAUTH_CLIENT_ID", "").strip()
ANTIGRAVITY_OAUTH_CLIENT_SECRET: str = os.getenv("ANTIGRAVITY_OAUTH_CLIENT_SECRET", "").strip()

# Working directory for chat turns (agent tasks choose their own).
WORKING_DIR: str = os.getenv("WORKING_DIR", "").strip() or os.getcwd()


# ============================================================
# SERVER
# ============================================================

API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
API_PORT: int = int(os.getenv("API_PORT", "8000"))

# Empty = open access. Set it before exposing the server beyond localhost.
API_KEY: str = os.getenv("API_KEY", "").strip()

# Web pages on other origins that may call this server from a browser. Empty =
# only the built-in UI (same origin). Never "*" without API_KEY: any website
# you visit could then read your tokens and run agent tasks.
CORS_ORIGINS: list[str] = _csv("CORS_ORIGINS", "")
# Host names the server answers to. Empty = this machine only (localhost,
# 127.0.0.1, [::1]) when API_HOST is a loopback address, else any.
TRUSTED_HOSTS: list[str] = [h.lower() for h in _csv("TRUSTED_HOSTS", "")]

# Preferred default model. If its provider is missing, the first model of
# whichever provider IS available is used instead.
DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "claude-sonnet-5").strip()


# ============================================================
# AUTO MODEL  (see router.py)
# ============================================================

# How model "auto" classifies a request:
#   heuristic  pattern signals only (instant)
#   llm        always ask a small fast model (~1-3 s extra)
#   hybrid     patterns for clear cases, the small model for unclear ones
AUTO_CLASSIFIER: str = os.getenv("AUTO_CLASSIFIER", "hybrid").strip().lower()
AUTO_CLASSIFIER_TIMEOUT: float = float(os.getenv("AUTO_CLASSIFIER_TIMEOUT", "12"))

# Models tried per request when one fails before answering (rate limit, crash).
AUTO_MAX_ATTEMPTS: int = int(os.getenv("AUTO_MAX_ATTEMPTS", "3"))

# Tell the model to use WebSearch / WebFetch on research requests.
AUTO_WEB_HINT: bool = _bool("AUTO_WEB_HINT", "1")

# Let "auto" hand very hard / multi-part requests to the orchestrator.
AUTO_ESCALATE: bool = _bool("AUTO_ESCALATE", "1")

# Orchestrator (see orchestrator.py).
ORCH_MAX_SUBTASKS: int = int(os.getenv("ORCH_MAX_SUBTASKS", "5"))
ORCH_REVIEW: bool = _bool("ORCH_REVIEW", "1")          # cross-check findings before synthesis
ORCH_STAGE_TIMEOUT: int = int(os.getenv("ORCH_STAGE_TIMEOUT", "240"))


# ============================================================
# PERFORMANCE
# ============================================================

# Keep this many pre-spawned claude.exe processes ready per recently used
# model, so a new conversation skips the ~2 s cold start. 0 disables.
CLAUDE_SPARE_PROCESSES: int = int(os.getenv("CLAUDE_SPARE_PROCESSES", "1"))
# Same for agy.exe, whose cold start is far worse (~12 s → ~2 s warm).
AGY_SPARE_PROCESSES: int = int(os.getenv("AGY_SPARE_PROCESSES", "1"))
# Cap on warm spares per provider across all models (each is ~120-290 MB).
SPARE_MAX_TOTAL: int = int(os.getenv("SPARE_MAX_TOTAL", "3"))

# Turns running at once per CLI provider. Each is a live process, so this is
# what bounds CPU / RAM. Extra requests wait in a queue.
CLAUDE_MAX_CONCURRENT: int = int(os.getenv("CLAUDE_MAX_CONCURRENT", "6"))
AGY_MAX_CONCURRENT: int = int(os.getenv("AGY_MAX_CONCURRENT", "4"))
# Seconds a request may wait for a free slot before it gets "server busy" (429).
QUEUE_TIMEOUT: float = float(os.getenv("QUEUE_TIMEOUT", "90"))
# Warm conversation processes kept per provider; the least recently used idle
# one is closed beyond this (its next turn restarts with the full transcript).
MAX_SESSIONS: int = int(os.getenv("MAX_SESSIONS", "12"))

# Chat turns run claude.exe with a short chat system prompt, only the chat
# tools, and no MCP servers / settings / on-disk session: ~2.3k instead of
# ~25k input tokens per turn. 0 = the full Claude Code agent prompt.
LEAN_CHAT: bool = _bool("LEAN_CHAT", "1")

# Pre-spawn the spares at boot (otherwise after the first request).
WARMUP_ON_STARTUP: bool = _bool("WARMUP_ON_STARTUP", "1")
# Models to pre-spawn at boot, comma-separated ("antigravity:gemini-3.8-flash-low,
# claude:claude-sonnet-5"). A provider not listed warms the default model, or
# its first one. At most SPARE_MAX_TOTAL per provider stay warm.
WARM_MODELS: list[str] = _csv("WARM_MODELS", "")

# Seconds an idle chat session is kept before being reaped.
SESSION_IDLE_TTL: int = int(os.getenv("SESSION_IDLE_TTL", str(15 * 60)))

# Max seconds for one chat turn.
TURN_TIMEOUT: int = int(os.getenv("TURN_TIMEOUT", "300"))

# Default reasoning effort (low | medium | high). Empty = binary decides.
# Deliberately NOT named CLAUDE_EFFORT: Claude Code exports that name into
# the environment and it would be silently inherited.
DEFAULT_EFFORT: str = os.getenv("SWITCHBOARD_EFFORT", "").strip()


# ============================================================
# TOOL PERMISSIONS
# ============================================================
# Non-interactive mode cannot answer permission prompts, so every tool must
# be pre-approved here. Anything listed is available to whoever can reach
# the server — keep chat read-only.

# claude.exe tools for plain chat.
CHAT_ALLOWED_TOOLS: str = os.getenv("CHAT_ALLOWED_TOOLS", "WebSearch,WebFetch").strip()

# claude.exe tools for /agent tasks. Requests may narrow this list, never widen it.
AGENT_ALLOWED_TOOLS: list[str] = _csv(
    "AGENT_ALLOWED_TOOLS", "Read,Glob,Grep,Write,Edit,WebSearch,WebFetch"
)

# Shell execution for agents (claude: Bash tool, agy: all permissions).
# NEVER enable on a server others can reach.
AGENT_ALLOW_SHELL: bool = _bool("AGENT_ALLOW_SHELL", "0")

AGENT_WORKING_DIR: str = os.getenv("AGENT_WORKING_DIR", "").strip() or WORKING_DIR

# Agent working_dir must sit inside one of these roots. Empty = any directory.
AGENT_ALLOWED_ROOTS: list[str] = _csv("AGENT_ALLOWED_ROOTS", "")

AGENT_TIMEOUT: int = int(os.getenv("AGENT_TIMEOUT", "900"))


# ============================================================
# STREAM SMOOTHING  (see pacing.py)
# ============================================================

SMOOTH_STREAM: bool = _bool("SMOOTH_STREAM", "1")
STREAM_TICK_MS: int = int(os.getenv("STREAM_TICK_MS", "25"))
STREAM_TARGET_DRAIN: float = float(os.getenv("STREAM_TARGET_DRAIN", "0.25"))
STREAM_FINAL_DRAIN: float = float(os.getenv("STREAM_FINAL_DRAIN", "0.10"))
STREAM_MIN_CPS: float = float(os.getenv("STREAM_MIN_CPS", "240"))
STREAM_MAX_CPS: float = float(os.getenv("STREAM_MAX_CPS", "3000"))
