"""
switchboard_ai.providers
────────────────────
Registry of providers and model routing.

A model can be named three ways:

    "gemini-3.8-flash-high"             bare id → whichever provider has it
    "antigravity:claude-sonnet-4-6"     provider-qualified (needed when both
    "claude:claude-sonnet-4-6"          providers expose the same id)
    model + {"provider": "antigravity"} explicit field

Bare ids that exist in several providers go to the first *available* one,
in registry order: the Claude API first when a key is set and
PREFER_CLAUDE_API=1, then Claude Code, then Antigravity.
"""

from __future__ import annotations

import difflib
import re
from typing import Optional

from switchboard_ai import config
from switchboard_ai.providers.antigravity import AntigravityProvider
from switchboard_ai.providers.base import Provider
from switchboard_ai.providers.claude import ClaudeProvider
from switchboard_ai.providers.claude_api import ClaudeAPIProvider

ALIASES = {
    "claude": "claude", "anthropic": "claude", "claude-code": "claude",
    "antigravity": "antigravity", "agy": "antigravity", "gemini": "antigravity",
    "claude-api": "claude-api", "api": "claude-api", "anthropic-api": "claude-api",
}

# Unlisted model ids are allowed when a provider is named explicitly (new
# models appear before this list is updated) — but only safe-looking ones,
# since the id becomes a command-line argument.
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}$")

# Antigravity ids carry the thinking level ("gemini-3.8-flash-high"). A bare
# base name resolves to the first of these that exists.
_VARIANT_ORDER = ("medium", "high", "low")


class RoutingError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Registry:
    def __init__(self) -> None:
        api = ClaudeAPIProvider()
        self.providers: dict[str, Provider] = {
            "claude": ClaudeProvider(),
            "antigravity": AntigravityProvider(),
        }
        if api.available:
            self.providers = ({"claude-api": api, **self.providers} if config.PREFER_CLAUDE_API
                              else {**self.providers, "claude-api": api})

    @property
    def api_preferred(self) -> bool:
        """Claude traffic goes to the API (multi-user safe) instead of claude.exe."""
        p = self.providers.get("claude-api")
        return bool(p and p.available and config.PREFER_CLAUDE_API)

    def get(self, pid: str) -> Optional[Provider]:
        return self.providers.get(ALIASES.get(pid.lower(), pid.lower()))

    @property
    def available(self) -> list[Provider]:
        return [p for p in self.providers.values() if p.available]

    def models(self) -> list[dict]:
        """Every model with a unique `key` ("provider:id") and availability."""
        # Virtual models, handled by router.py / orchestrator.py.
        up = bool(self.available)
        out = [
            {"id": "auto", "name": "Auto", "tier": "Router", "provider": "auto", "key": "auto",
             "available": up,
             "description": "Routes each message to the best model; hard multi-part requests escalate to the orchestrator."},
            {"id": "auto-orchestrate", "name": "Auto Orchestrator", "tier": "Multi-model", "provider": "auto",
             "key": "auto-orchestrate", "available": up,
             "description": "Planner → parallel specialist models (with web research) → cross-review → synthesis."},
            {"id": "auto-ensemble", "name": "Auto Ensemble", "tier": "Mixture-of-Agents", "provider": "auto",
             "key": "auto-ensemble", "available": up,
             "description": "Claude, Gemini and GPT-OSS answer independently; a strong model merges the best of each."},
        ]
        for p in self.providers.values():
            for m in p.models():
                out.append({**m, "key": f"{p.id}:{m['id']}", "available": p.available})
        return out

    def default(self) -> tuple[Optional[Provider], Optional[str]]:
        try:
            return self.resolve(config.DEFAULT_MODEL, None)
        except RoutingError:
            pass
        for p in self.available:
            models = p.models()
            if models:
                return p, models[0]["id"]
        return None, None

    def resolve(self, model: Optional[str], provider: Optional[str]) -> tuple[Provider, str]:
        model = (model or "").strip()

        if model and ":" in model:
            prefix, rest = model.split(":", 1)
            if self.get(prefix):
                provider, model = prefix, rest

        if not model:
            p, m = self.default()
            if p is None:
                raise RoutingError(503, self._none_available())
            if provider and self.get(provider) is not p:
                models = self._need(provider).models()
                if not models:
                    raise RoutingError(503, f"{provider} has no models")
                return self._need(provider), models[0]["id"]
            return p, m

        if provider:
            p = self._need(provider)
            known = any(m["id"] == model for m in p.models())
            if not known:
                variant = self._variant(model)
                if variant and variant[0] is p:
                    return variant
            if not known and not _SAFE_MODEL.match(model):
                raise RoutingError(400, f"Invalid model id: {model!r}")
            return p, model

        owners = [p for p in self.providers.values() if any(m["id"] == model for m in p.models())]
        if not owners:
            variant = self._variant(model)
            if variant:
                return variant
            ids = [m["id"] for p in self.providers.values() for m in p.models()]
            close = difflib.get_close_matches(model, ids, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise RoutingError(400, f"Unknown model {model!r}.{hint} See GET /models for every id.")
        for p in owners:
            if p.available:
                return p, model
        raise RoutingError(503, f"{model} needs {owners[0].label}, which is not installed or disabled.")

    def _variant(self, model: str) -> Optional[tuple[Provider, str]]:
        """A base name like "gemini-3.8-flash" → its -medium (else -high / -low) variant."""
        for level in _VARIANT_ORDER:
            for p in self.available:
                if any(m["id"] == f"{model}-{level}" for m in p.models()):
                    return p, f"{model}-{level}"
        return None

    def _need(self, pid: str) -> Provider:
        p = self.get(pid)
        if p is None:
            raise RoutingError(400, f"Unknown provider {pid!r}. Use one of: {', '.join(self.providers)}")
        if not p.available:
            raise RoutingError(503, f"{p.label} is not installed or disabled on this machine.")
        return p

    def _none_available(self) -> str:
        return (
            "No active AI provider available. Authenticate via Claude OAuth or Google Antigravity "
            "on the dashboard at http://localhost:8000, or provide an API key."
        )


registry = Registry()
