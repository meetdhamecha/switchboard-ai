"""
switchboard_ai.providers.claude_api
───────────────────────────────
The official Claude API (Messages API, `anthropic` SDK) with a Claude
Console API key: pay-per-token, many parallel requests, and the permitted
way to serve other users (the claude.exe provider runs on a personal
Pro/Max login, which Anthropic's terms reserve for individual use).

Enabled when SWITCHBOARD_ANTHROPIC_API_KEY is set. No processes: each request is
one streamed HTTPS call, so hundreds can run at once; CLAUDE_API_MAX_CONCURRENT
bounds them and the SDK retries 429 / 5xx with backoff.

    • adaptive thinking + output_config.effort (Haiku 4.5: neither)
    • server-side web_search / web_fetch; tool calls and results are
      emitted as tool_use / tool_result events, cited URLs are appended
    • pause_turn (long server-tool loops) is continued automatically
    • refusal fallbacks on Claude Fable 5.1 / Opus 5
    • automatic prompt caching (top-level cache_control)
    • optional server-side memory per session_id, so clients may send only
      the new message after the first turn (same contract as the CLIs)
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import AsyncGenerator, Optional

from switchboard_ai import config
from switchboard_ai.process import Busy, Msg, Slots, busy_event
from switchboard_ai.providers.base import Provider, chat_system_prompt

MODELS: list[dict] = [
    {"id": "claude-opus-5-5",  "name": "Claude Opus 5.5",  "tier": "Flagship"},
    {"id": "claude-fable-5-1", "name": "Claude Fable 5.1", "tier": "Most capable"},
    {"id": "claude-opus-5",    "name": "Claude Opus 5",    "tier": "Flagship"},
    {"id": "claude-sonnet-5",  "name": "Claude Sonnet 5",  "tier": "Balanced"},
    {"id": "claude-opus-4-8",  "name": "Claude Opus 4.8",  "tier": "Flagship"},
    {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "tier": "Fast"},
]

# CLI ids that differ from the API ids.
CLI_TO_API = {"claude-haiku-4-5-20251001": "claude-haiku-4-5"}

# Web tools with dynamic filtering exist on these; others use the basic ones.
_DYNAMIC_WEB = {"claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5"}
# Haiku 4.5 takes no effort parameter and no adaptive thinking.
_NO_EFFORT = {"claude-haiku-4-5"}
# Server-side refusal fallbacks (beta) — on by default for these models.
_FALLBACKS = {"claude-fable-5-1", "claude-opus-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: (input, output). Cache reads bill ~0.1x input,
# cache writes ~1.25x input. Used for the cost_usd estimate only.
_PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-fable-5-1": (10.0, 50.0), "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-opus-4-8": (5.0, 25.0), "claude-haiku-4-5": (1.0, 5.0),
}

_MAX_CONTINUATIONS = 4        # pause_turn resumptions per request
_HISTORY_MAX = 2000           # remembered session transcripts


def _cost(model: str, u: dict) -> float:
    pin, pout = _PRICES.get(model, (0.0, 0.0))
    tokens_in = (u.get("input_tokens") or 0) + 0.1 * (u.get("cache_read_input_tokens") or 0) \
        + 1.25 * (u.get("cache_creation_input_tokens") or 0)
    return round((tokens_in * pin + (u.get("output_tokens") or 0) * pout) / 1_000_000, 6)


def _tool_event(block) -> Optional[dict]:
    """Map a finished server-tool block to a normalized event."""
    kind = getattr(block, "type", "")
    if kind == "server_tool_use":
        inp = getattr(block, "input", None)
        inp = inp if isinstance(inp, dict) else {}
        name = {"web_search": "WebSearch", "web_fetch": "WebFetch"}.get(block.name, block.name)
        detail = inp.get("query") or inp.get("url") or ""
        return {"type": "tool_use", "id": block.id, "name": name, "detail": str(detail)[:140]}
    if kind in ("web_search_tool_result", "web_fetch_tool_result"):
        content = getattr(block, "content", None)
        ctype = "" if isinstance(content, list) else str(getattr(content, "type", ""))
        err = "error" in ctype
        code = getattr(content, "error_code", "") if err else ""
        return {"type": "tool_result", "id": block.tool_use_id, "ok": not err, "content": str(code or "")}
    return None


def _sources(content) -> str:
    """Markdown list of the URLs the answer cites (web search citations)."""
    seen: dict[str, str] = {}
    for block in content or []:
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in seen:
                seen[url] = getattr(c, "title", None) or url
    if not seen:
        return ""
    return "\n\n**Sources:**\n" + "\n".join(f"- [{t}]({u})" for u, t in list(seen.items())[:10])


class ClaudeAPIProvider(Provider):
    id = "claude-api"
    label = "Claude API"
    efforts = ("low", "medium", "high", "xhigh", "max")

    def __init__(self) -> None:
        super().__init__()
        self.binary = None
        self.enabled = bool(config.CLAUDE_API_KEY)
        self.slots = Slots(config.CLAUDE_API_MAX_CONCURRENT, config.QUEUE_TIMEOUT)
        self._client = None
        self._history: OrderedDict[str, tuple[list[dict], float]] = OrderedDict()
        if self.enabled:
            try:
                from anthropic import AsyncAnthropic
            except ImportError:
                self.enabled = False
                print("  [claude-api] SWITCHBOARD_ANTHROPIC_API_KEY is set but the `anthropic` package "
                      "is missing: pip install anthropic")
            else:
                self._client = AsyncAnthropic(
                    api_key=config.CLAUDE_API_KEY, max_retries=3, timeout=float(config.TURN_TIMEOUT),
                )

    # ── discovery ───────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self.enabled and self._client is not None

    def info(self) -> dict:
        return {
            **super().info(),
            "binary": None,
            "logged_in": self.available,
            "account": {"auth": "API key (Claude Console)", "billing": "pay per token"} if self.available else None,
            "concurrency": self.slots.stats(),
            "web_tools": config.CLAUDE_API_WEB_TOOLS,
        }

    def models(self) -> list[dict]:
        return [{**m, "provider": self.id, "efforts": list(self.model_efforts(m["id"]))} for m in MODELS]

    def model_efforts(self, model: str) -> tuple[str, ...]:
        return () if CLI_TO_API.get(model, model) in _NO_EFFORT else self.efforts

    async def startup(self, warm_models: list[str]) -> None:
        return None

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()

    # ── request building ────────────────────────────────────

    def _conversation(self, messages: list[Msg], session_id: Optional[str]) -> tuple[list[dict], str]:
        system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content.strip())
        turns = [{"role": m.role, "content": m.content} for m in messages
                 if m.role in ("user", "assistant") and m.content.strip()]
        # With a session, a client may send only the new message after turn one.
        if session_id and session_id in self._history and len(turns) == 1 and turns[0]["role"] == "user":
            turns = self._history[session_id][0] + turns
        while turns and turns[0]["role"] != "user":   # the API requires a user turn first
            turns.pop(0)
        return turns, system

    def _remember(self, session_id: str, turns: list[dict], answer: str) -> None:
        self._history[session_id] = (turns + [{"role": "assistant", "content": answer}], time.monotonic())
        self._history.move_to_end(session_id)
        cutoff = time.monotonic() - config.SESSION_IDLE_TTL
        while self._history and (len(self._history) > _HISTORY_MAX
                                 or next(iter(self._history.values()))[1] < cutoff):
            self._history.popitem(last=False)

    def _params(self, model: str, turns: list[dict], system: str, effort: Optional[str]) -> dict:
        name = next((m["name"] for m in MODELS if m["id"] == model), model)
        web = config.CLAUDE_API_WEB_TOOLS
        system_text = chat_system_prompt(name, model, web) + (f"\n\n{system}" if system else "")
        params: dict = {
            "model": model,
            "max_tokens": 32000 if model in _NO_EFFORT else 64000,
            "system": system_text,
            "messages": turns,
            "cache_control": {"type": "ephemeral"},   # automatic prompt caching
        }
        if model not in _NO_EFFORT:
            params["thinking"] = {"type": "adaptive"}
            if effort:
                params["output_config"] = {"effort": effort}
        if web:
            dynamic = model in _DYNAMIC_WEB
            params["tools"] = [
                {"type": "web_search_20260209" if dynamic else "web_search_20250305",
                 "name": "web_search", "max_uses": 5},
                {"type": "web_fetch_20260209" if dynamic else "web_fetch_20250910",
                 "name": "web_fetch", "max_uses": 5},
            ]
        return params

    # ── work ────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[Msg],
        model: str,
        session_id: Optional[str],
        effort: Optional[str],
    ) -> AsyncGenerator[dict, None]:
        import anthropic

        model, effort = self.resolve_effort(CLI_TO_API.get(model, model), effort)
        turns, system = self._conversation(messages, session_id)
        if not turns:
            yield {"type": "error", "content": "no user message to answer"}
            return
        params = self._params(model, turns, system, effort)
        use_fallbacks = model in _FALLBACKS
        usage = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        text: list[str] = []
        t0 = time.monotonic()
        final = None

        try:
            async with self.slots.hold():
                for _ in range(_MAX_CONTINUATIONS):
                    if use_fallbacks:
                        stream_cm = self._client.beta.messages.stream(
                            **params, betas=[_FALLBACK_BETA], fallbacks="default")
                    else:
                        stream_cm = self._client.messages.stream(**params)
                    async with stream_cm as stream:
                        async for ev in stream:
                            if ev.type == "text" and ev.text:
                                text.append(ev.text)
                                yield {"type": "text", "content": ev.text}
                            elif ev.type == "content_block_stop":
                                tool = _tool_event(ev.content_block)
                                if tool:
                                    yield tool
                        final = await stream.get_final_message()
                    for k in usage:
                        usage[k] += getattr(final.usage, k, 0) or 0
                    if final.stop_reason != "pause_turn":
                        break
                    # A long server-tool loop paused: send the turn back to continue it.
                    params["messages"] = params["messages"] + [
                        {"role": "assistant", "content": [b.to_dict() for b in final.content]}]
        except Busy as e:
            yield busy_event(e)
            return
        except anthropic.RateLimitError as e:
            wait = e.response.headers.get("retry-after", "")
            yield {"type": "error", "code": "rate_limited",
                   "content": f"Claude API rate limit reached{f'; retry after {wait}s' if wait else ''}."}
            return
        except anthropic.APIStatusError as e:
            yield {"type": "error", "content": f"Claude API error {e.status_code}: {e.message}"}
            return
        except anthropic.APIConnectionError as e:
            yield {"type": "error", "content": f"Could not reach the Claude API: {e}"}
            return

        stop = final.stop_reason if final else "unknown"
        if stop == "refusal":
            yield {"type": "notice", "content": "The model declined this request."}
        elif stop == "max_tokens":
            yield {"type": "notice", "content": "The answer hit the output token limit and was cut off."}
        cited = _sources(final.content) if final else ""
        if cited and "](" not in "".join(text)[-2000:]:
            text.append(cited)
            yield {"type": "text", "content": cited}
        answer = "".join(text).strip()
        if session_id and answer:
            self._remember(session_id, turns, answer)
        yield {
            "type": "result",
            "content": answer,
            "usage": usage,
            "cost_usd": _cost(model, usage),
            "stop_reason": stop,
            "model": getattr(final, "model", model) if final else model,
            "native_session_id": getattr(final, "id", "") if final else "",
            "duration_ms": round((time.monotonic() - t0) * 1000),
        }

    def agent(self, task, model, working_dir, tools, resume, effort) -> AsyncGenerator[dict, None]:
        async def unsupported():
            yield {"type": "error", "content": "Agent tasks (local file tools) need a CLI provider: "
                                               "use claude:<model> or antigravity:<model>."}
        return unsupported()

    def agent_tools(self) -> list[str]:
        return []
