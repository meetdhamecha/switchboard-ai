"""
switchboard_ai.providers.claude
───────────────────────────
claude.exe — the Claude Code binary shipped inside the Claude Code IDE
extension, signed in with a Claude Pro/Max account.

Output format (--output-format stream-json --include-partial-messages):

    stream_event{content_block_start: tool_use}   → tool starts
    stream_event{content_block_delta: text_delta} → incremental text
    assistant{message}                            → complete message (text is a
                                                    duplicate of the deltas;
                                                    tool inputs are complete here)
    user{tool_result}                             → tool finished
    result                                        → turn done, usage, session_id
"""

from __future__ import annotations

import json
from typing import AsyncGenerator, Optional

from switchboard_ai import config
from switchboard_ai.discovery import find_claude_binaries, resolve
from switchboard_ai.process import Msg, SessionPool, run_once
from switchboard_ai.providers.base import UUID_RE, Provider, chat_system_prompt, tool_detail

MODELS: list[dict] = [
    {"id": "claude-opus-5-5",            "name": "Claude Opus 5.5",   "tier": "Flagship"},
    {"id": "claude-fable-5-1",           "name": "Claude Fable 5.1",  "tier": "Creative"},
    {"id": "claude-opus-5",              "name": "Claude Opus 5",     "tier": "Flagship"},
    {"id": "claude-sonnet-5",            "name": "Claude Sonnet 5",   "tier": "Balanced"},
    {"id": "claude-fable-5",             "name": "Claude Fable 5",    "tier": "Creative"},
    {"id": "claude-opus-4-8",            "name": "Claude Opus 4.8",   "tier": "Flagship"},
    {"id": "claude-opus-4-7",            "name": "Claude Opus 4.7",   "tier": "Flagship"},
    {"id": "claude-sonnet-4-6",          "name": "Claude Sonnet 4.6", "tier": "Balanced"},
    {"id": "claude-opus-4-6",            "name": "Claude Opus 4.6",   "tier": "Flagship"},
    {"id": "claude-opus-4-5-20251101",   "name": "Claude Opus 4.5",   "tier": "Flagship"},
    {"id": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5", "tier": "Balanced"},
    {"id": "claude-haiku-4-5-20251001",  "name": "Claude Haiku 4.5",  "tier": "Fast"},
]


def _parser():
    """Stateful per-turn parser: one stream-json line → normalized events."""
    streamed_text = False
    errored = False            # a synthetic error message was already reported
    seen_tools: set[str] = set()

    def blocks(message) -> list[dict]:
        out: list[dict] = []
        content = message.get("content", []) if isinstance(message, dict) else []
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text") and not streamed_text:
                out.append({"type": "text", "content": b["text"]})
            elif b.get("type") == "tool_use":
                # Re-emitted with the full input; clients upsert by id.
                tid = b.get("id") or b.get("name", "tool")
                seen_tools.add(tid)
                out.append({
                    "type": "tool_use", "id": tid, "name": b.get("name", "tool"),
                    "detail": tool_detail(b.get("input")),
                })
        return out

    def parse(line: str) -> list[dict]:
        nonlocal streamed_text, errored
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(ev, dict):
            return []
        kind = ev.get("type", "")

        if kind == "stream_event":
            inner = ev.get("event") or {}
            itype = inner.get("type")
            if itype == "content_block_delta":
                delta = inner.get("delta") or {}
                # thinking / signature / input_json deltas are not user-visible.
                if delta.get("type") == "text_delta" and delta.get("text"):
                    streamed_text = True
                    return [{"type": "text", "content": delta["text"]}]
            elif itype == "content_block_start":
                block = inner.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tid = block.get("id") or block.get("name", "tool")
                    seen_tools.add(tid)
                    return [{"type": "tool_use", "id": tid, "name": block.get("name", "tool"), "detail": ""}]
            elif itype == "message_start":
                # A new assistant message (e.g. after a tool call) — separate
                # it visually from the text before the tool.
                if streamed_text:
                    return [{"type": "text", "content": "\n\n"}]
            return []

        if kind == "assistant":
            message = ev.get("message") or {}
            if ev.get("error") or message.get("model") == "<synthetic>":
                # Not a model answer: claude.exe's own notice (usage limit,
                # out of credits, …). Surface it as an error so callers can
                # fail over instead of showing it as the reply.
                errored = True
                note = " ".join(b.get("text", "") for b in message.get("content") or []
                                if isinstance(b, dict) and b.get("type") == "text").strip()
                err = {"type": "error", "content": note or f"claude.exe error: {ev.get('error')}"}
                if ev.get("error") == "rate_limit":
                    err["code"] = "rate_limited"
                return [err]
            return blocks(message)

        if kind == "user":
            out = []
            content = (ev.get("message") or {}).get("content", [])
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    body = b.get("content")
                    if isinstance(body, list):
                        body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                    ok = not b.get("is_error")
                    out.append({
                        "type": "tool_result", "id": b.get("tool_use_id", ""), "ok": ok,
                        "content": "" if ok else str(body or "")[:300],
                    })
            return out

        if kind == "result":
            out: list[dict] = []
            text = ev.get("result") or ""
            if not streamed_text and text and not ev.get("is_error"):
                out.append({"type": "text", "content": text})
            for denied in ev.get("permission_denials") or []:
                name = denied.get("tool_name", "a tool") if isinstance(denied, dict) else str(denied)
                out.append({"type": "notice", "content": f"{name} was blocked — it is not in the allowed tools list."})
            if ev.get("is_error") and not errored:
                err = {"type": "error", "content": text or ev.get("subtype", "claude.exe reported an error")}
                if ev.get("api_error_status") == 429:
                    err["code"] = "rate_limited"
                out.append(err)
            usage = ev.get("usage") or {}
            out.append({
                "type": "result",
                "content": "" if ev.get("is_error") else text,
                "usage": {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                },
                "cost_usd": ev.get("total_cost_usd") or 0,
                "stop_reason": ev.get("stop_reason") or "end_turn",
                "native_session_id": ev.get("session_id", ""),
                "duration_ms": ev.get("duration_ms"),
            })
            return out

        if kind == "error":
            return [{"type": "error", "content": str(ev.get("message", ev))}]

        return []  # system / rate_limit_event / …

    return parse


class ClaudeProvider(Provider):
    id = "claude"
    label = "Claude Code"
    efforts = ("low", "medium", "high", "xhigh", "max")

    def __init__(self) -> None:
        super().__init__()
        self.enabled = config.ENABLE_CLAUDE
        self.binary = resolve(config.CLAUDE_BINARY_PATH, find_claude_binaries)
        if self.available:
            self.pool = SessionPool(
                driver=self,
                command=self._chat_command,
                cwd=config.WORKING_DIR,
                spares=config.CLAUDE_SPARE_PROCESSES,
                idle_ttl=config.SESSION_IDLE_TTL,
                timeout=config.TURN_TIMEOUT,
                max_concurrent=config.CLAUDE_MAX_CONCURRENT,
                queue_timeout=config.QUEUE_TIMEOUT,
                max_sessions=config.MAX_SESSIONS,
                spare_max_total=config.SPARE_MAX_TOTAL,
            )

    # ── Driver protocol ─────────────────────────────────────

    def encode(self, text: str) -> bytes:
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        return (json.dumps(msg) + "\n").encode("utf-8")

    def new_parser(self):
        return _parser()

    def _base(self, model: str, effort: str) -> list[str]:
        cmd = [
            self.binary, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--model", model,
        ]
        if effort:
            cmd += ["--effort", effort]
        return cmd

    def _chat_command(self, model: str, effort: str) -> list[str]:
        cmd = self._base(model, effort or config.DEFAULT_EFFORT)
        # --allowedTools / --tools are variadic; safe here because no
        # positional prompt follows (the prompt goes over stdin).
        if config.CHAT_ALLOWED_TOOLS:
            cmd += ["--allowedTools", config.CHAT_ALLOWED_TOOLS]
        if config.LEAN_CHAT:
            # Chat needs none of the coding-agent machinery: a short system
            # prompt, only the chat tools, no MCP servers, no user/project
            # settings (hooks, plugins) and no session files on disk.
            # Measured: ~25.7k → ~2.3k input tokens per turn.
            name = next((m["name"] for m in MODELS if m["id"] == model), model)
            web = any(t in config.CHAT_ALLOWED_TOOLS for t in ("WebSearch", "WebFetch"))
            cmd += [
                "--system-prompt", chat_system_prompt(name, model, web),
                "--tools", config.CHAT_ALLOWED_TOOLS,
                "--strict-mcp-config",
                "--setting-sources", "",
                "--no-session-persistence",
                "--disable-slash-commands",
            ]
        return cmd

    # ── Provider API ────────────────────────────────────────

    def info(self) -> dict:
        from switchboard_ai.auth import ClaudeAuth
        cred = ClaudeAuth.get_credentials(reveal=False)
        return {
            **super().info(),
            "logged_in": cred.get("logged_in", False),
            "account": {
                "email": cred.get("email"),
                "subscription": cred.get("subscription"),
                "rate_limit_tier": cred.get("rate_limit_tier"),
                "organization_uuid": cred.get("organization_uuid"),
                "access_token": cred.get("access_token"),
                "access_token_expires_in": cred.get("access_token_expires_in"),
                "refresh_token": cred.get("refresh_token"),
                "refresh_token_expires_in": cred.get("refresh_token_expires_in"),
            } if cred.get("available") else None,
        }


    def models(self) -> list[dict]:
        return [{**m, "provider": self.id} for m in MODELS]

    def agent_tools(self) -> list[str]:
        tools = list(config.AGENT_ALLOWED_TOOLS)
        if config.AGENT_ALLOW_SHELL and "Bash" not in tools:
            tools.append("Bash")
        return tools

    def agent(
        self,
        task: str,
        model: str,
        working_dir: str,
        tools: Optional[list[str]],
        resume: Optional[str],
        effort: Optional[str],
    ) -> AsyncGenerator[dict, None]:
        allowed = self.agent_tools()
        # A request can narrow the configured tool list, never widen it.
        chosen = [t for t in (tools or allowed) if t in allowed]
        cmd = self._base(model, self.effort(effort) or config.DEFAULT_EFFORT)
        if chosen:
            cmd += ["--allowedTools", ",".join(chosen)]
        if resume and UUID_RE.match(resume):
            cmd += ["--resume", resume]
        return run_once(self, cmd, working_dir, model, [Msg("user", task)], config.AGENT_TIMEOUT,
                        self.pool.slots if self.pool else None)

