"""
switchboard_ai.providers.base
─────────────────────────
Common shape of a provider, and the normalized event format every
provider's parser emits:

    {"type": "text",        "content": str}
    {"type": "tool_use",    "id": str, "name": str, "detail": str}
    {"type": "tool_result", "id": str, "ok": bool, "content": str}
    {"type": "notice",      "content": str}
    {"type": "result",      "content", "usage", "cost_usd", "stop_reason",
                            "model", "native_session_id", "duration_ms"}
    {"type": "error",       "content": str}

The server, pacing and UI only ever see these.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import AsyncGenerator, Optional

from switchboard_ai import config
from switchboard_ai.process import Msg, SessionPool

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Every reasoning-effort level a request may name, lowest to highest.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def nearest_effort(want: str, allowed: tuple[str, ...]) -> str:
    """`want` if allowed, else the closest allowed level (a tie goes to the higher one)."""
    if want in allowed:
        return want
    if not allowed:
        return ""
    rank = EFFORT_LEVELS.index
    return min(allowed, key=lambda a: (abs(rank(a) - rank(want)), -rank(a)))

_DETAIL_KEYS = (
    "file_path", "path", "TargetFile", "AbsolutePath", "DirectoryPath",
    "pattern", "Query", "query", "command", "CommandLine", "url", "Url",
    "description", "prompt",
)


def chat_system_prompt(model_name: str, model_id: str, web: bool) -> str:
    """
    Short system prompt for chat turns. Replaces Claude Code's ~23k-token
    coding-agent prompt, which also stated which model is running, so that
    fact is restated here.
    """
    today = datetime.now().strftime("%A, %B %d, %Y")
    prompt = (
        f"You are {model_name} (model id: {model_id}), an AI model made by Anthropic, "
        f"answering through the Switchboard AI chat. Today's date is {today}.\n"
        "Answer the user's latest message directly, accurately and helpfully. "
        "Use Markdown (headings, lists, tables, fenced code with a language tag) "
        "when it makes the answer easier to read. If you are unsure, say so rather than guess."
    )
    if web:
        prompt += (
            "\nYou have web search and web fetch tools. Use them when the answer depends on "
            "current or external information, or when the user gives a URL, and cite the "
            "sources you used as links."
        )
    return prompt


def tool_detail(params) -> str:
    """One short human-readable line describing a tool call's input."""
    if not isinstance(params, dict):
        return ""
    for key in _DETAIL_KEYS:
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            val = " ".join(val.split())
            return val if len(val) <= 140 else val[:137] + "…"
    return ""


class Provider:
    id: str = ""
    label: str = ""
    efforts: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.binary: Optional[str] = None
        self.pool: Optional[SessionPool] = None
        self.enabled = True

    # ── discovery ───────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.binary)

    def info(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "enabled": self.enabled,
            "available": self.available,
            "binary": self.binary,
            "efforts": list(self.efforts),
        }

    def models(self) -> list[dict]:
        raise NotImplementedError

    def model_efforts(self, model: str) -> tuple[str, ...]:
        """Effort levels this model accepts (empty = it has no effort setting)."""
        return self.efforts

    def resolve_effort(self, model: str, effort: Optional[str]) -> tuple[str, str]:
        """(model, effort) to actually run: the requested level, else
        SWITCHBOARD_EFFORT, moved to the nearest level the model supports.
        Calling it again on its own result changes nothing."""
        want = (effort or config.DEFAULT_EFFORT).strip().lower()
        if want not in EFFORT_LEVELS:
            return model, ""
        return model, nearest_effort(want, self.model_efforts(model))

    # ── lifecycle ───────────────────────────────────────────

    async def startup(self, warm_models: list[str]) -> None:
        if self.pool:
            self.pool.start_reaper()
            for model in warm_models:
                await self.pool.warmup(*self.resolve_effort(model, None))

    async def shutdown(self) -> None:
        if self.pool:
            await self.pool.stop_reaper()
            await self.pool.close_all()

    # ── work ────────────────────────────────────────────────

    def chat(
        self,
        messages: list[Msg],
        model: str,
        session_id: Optional[str],
        effort: Optional[str],
    ) -> AsyncGenerator[dict, None]:
        model, effort = self.resolve_effort(model, effort)
        return self.pool.ask(session_id, messages, model, effort)

    def agent(
        self,
        task: str,
        model: str,
        working_dir: str,
        tools: Optional[list[str]],
        resume: Optional[str],
        effort: Optional[str],
    ) -> AsyncGenerator[dict, None]:
        raise NotImplementedError

    def agent_tools(self) -> list[str]:
        raise NotImplementedError
