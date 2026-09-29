"""
switchboard_ai.providers.antigravity
────────────────────────────────
agy.exe — the Antigravity CLI (~/.gemini/bin/agy.exe), signed in with the
Google account used by the Antigravity IDE. Gives Gemini, GPT-OSS and
Claude "Thinking" models.

Input  (--input-format stream-json):  {"event": "user", "message": {...}}\n
Output (--output-format stream-json):

    step_update{step_type: agent_response, text_delta}   → incremental text
    step_update{step_type: tool, state: ACTIVE|DONE|ERROR} → tool lifecycle
    result{status, response, usage, conversation_id,
           denied_actions}                                → turn done

Note: `-p` takes a value, so print mode with stdin input is spelled `-p=`.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncGenerator, Optional

from switchboard_ai import config
from switchboard_ai.discovery import find_agy_binaries, resolve
from switchboard_ai.process import Msg, SessionPool, run_once
from switchboard_ai.providers.base import UUID_RE, Provider, tool_detail

# Used until `agy models` answers (or if it cannot be reached).
FALLBACK_MODELS: list[dict] = [
    {"id": "gemini-3.8-flash-high",    "name": "Gemini 3.8 Flash (High)"},
    {"id": "gemini-3.8-flash-medium",  "name": "Gemini 3.8 Flash (Medium)"},
    {"id": "gemini-3.8-flash-low",     "name": "Gemini 3.8 Flash (Low)"},
    {"id": "gemini-3.7-flash-high",    "name": "Gemini 3.7 Flash (High)"},
    {"id": "gemini-3.7-flash-medium",  "name": "Gemini 3.7 Flash (Medium)"},
    {"id": "gemini-3.7-flash-low",     "name": "Gemini 3.7 Flash (Low)"},
    {"id": "gemini-3.6-flash-high",    "name": "Gemini 3.6 Flash (High)"},
    {"id": "gemini-3.6-flash-medium",  "name": "Gemini 3.6 Flash (Medium)"},
    {"id": "gemini-3.6-flash-low",     "name": "Gemini 3.6 Flash (Low)"},
    {"id": "gemini-3.1-pro-high",      "name": "Gemini 3.1 Pro (High)"},
    {"id": "gemini-3.1-pro-low",       "name": "Gemini 3.1 Pro (Low)"},
    {"id": "claude-sonnet-4-6",        "name": "Claude Sonnet 4.6 (Thinking)"},
    {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6 (Thinking)"},
    {"id": "gpt-oss-120b-medium",      "name": "GPT-OSS 120B (Medium)"},
]


def _tier(name: str) -> str:
    m = re.search(r"\(([^)]+)\)\s*$", name)
    return m.group(1) if m else ""


def _parser():
    streamed_text = False
    turn_usage = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    last_response_step = None

    def parse(line: str) -> list[dict]:
        nonlocal streamed_text, last_response_step
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(ev, dict):
            return []
        kind = ev.get("event", "")

        if kind == "step_update":
            su = ev.get("step_update") or {}
            stype, state = su.get("step_type"), su.get("state")
            step = str(su.get("step_index", ""))
            out: list[dict] = []

            if stype == "agent_response":
                delta = su.get("text_delta") or ""
                if delta:
                    # Separate text blocks that are split by tool calls.
                    if streamed_text and last_response_step not in (None, step) and not delta.startswith("\n"):
                        out.append({"type": "text", "content": "\n\n"})
                    streamed_text = True
                    last_response_step = step
                    out.append({"type": "text", "content": delta})
                if state == "DONE":
                    for k in turn_usage:
                        turn_usage[k] += (su.get("usage") or {}).get(k) or 0

            elif stype == "tool":
                info = su.get("tool_info") or {}
                name = su.get("tool_name") or info.get("name") or "tool"
                tid = f"step-{step}"
                if state == "ACTIVE":
                    out.append({"type": "tool_use", "id": tid, "name": name,
                                "detail": tool_detail(info.get("parameters"))})
                elif state in ("DONE", "ERROR"):
                    err = (info.get("error") or {}).get("message", "")
                    out.append({"type": "tool_result", "id": tid, "ok": state == "DONE",
                                "content": err[:300]})
            return out

        if kind == "result":
            r = ev.get("result") or {}
            out = []
            text = r.get("response") or ""
            if r.get("status") not in (None, "SUCCESS"):
                out.append({"type": "error", "content": r.get("error") or f"agy status: {r.get('status')}"})
            elif not streamed_text and text:
                out.append({"type": "text", "content": text})
            for d in r.get("denied_actions") or []:
                label = d.get("display_name") or d.get("action") or "an action"
                out.append({"type": "notice",
                            "content": f"{label} was blocked — headless mode cannot ask for permission."})
            dur = r.get("duration_seconds")
            out.append({
                "type": "result",
                "content": text,
                "usage": {k: v or None for k, v in turn_usage.items()},
                "cost_usd": 0.0,
                "stop_reason": "end_turn",
                "native_session_id": r.get("conversation_id", ""),
                "duration_ms": round(dur * 1000) if isinstance(dur, (int, float)) else None,
            })
            return out

        return []  # init, …

    return parse


class AntigravityProvider(Provider):
    id = "antigravity"
    label = "Antigravity"
    efforts = ("low", "medium", "high")

    def __init__(self) -> None:
        super().__init__()
        self.enabled = config.ENABLE_ANTIGRAVITY
        self.binary = resolve(config.AGY_BINARY_PATH, find_agy_binaries)
        self._models = [dict(m) for m in FALLBACK_MODELS]
        self.models_source = "builtin"
        if self.available:
            self.pool = SessionPool(
                driver=self,
                command=self._chat_command,
                cwd=config.WORKING_DIR,
                spares=config.AGY_SPARE_PROCESSES,
                idle_ttl=config.SESSION_IDLE_TTL,
                timeout=config.TURN_TIMEOUT,
                max_concurrent=config.AGY_MAX_CONCURRENT,
                queue_timeout=config.QUEUE_TIMEOUT,
                max_sessions=config.MAX_SESSIONS,
                spare_max_total=config.SPARE_MAX_TOTAL,
            )

    # ── Driver protocol ─────────────────────────────────────

    def encode(self, text: str) -> bytes:
        msg = {"event": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        return (json.dumps(msg) + "\n").encode("utf-8")

    def new_parser(self):
        return _parser()

    def _base(self, model: str, effort: str) -> list[str]:
        cmd = [
            self.binary, "-p=",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--disable-slash-commands",
            "--model", model,
        ]
        if effort:
            cmd += ["--effort", effort]
        return cmd

    def _chat_command(self, model: str, effort: str) -> list[str]:
        # No permission flags: anything that needs approval is auto-denied.
        return self._base(model, effort or self.effort(config.DEFAULT_EFFORT))

    # ── Provider API ────────────────────────────────────────

    async def refresh_models(self) -> None:
        """Ask agy for its live model list; keep the builtin one on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "models",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:
            return
        models = []
        for line in out.decode("utf-8", errors="replace").splitlines():
            if "\t" in line:
                mid, name = line.split("\t", 1)
                if mid.strip():
                    models.append({"id": mid.strip(), "name": name.strip()})
        if models:
            self._models = models
            self.models_source = "agy models"

    async def startup(self, warm_model: Optional[str]) -> None:
        await self.refresh_models()
        await super().startup(warm_model)

    def info(self) -> dict:
        from switchboard_ai.auth import AntigravityAuth
        cred = AntigravityAuth.get_credentials(reveal=False)
        return {
            **super().info(),
            "logged_in": cred.get("logged_in", False),
            "models_source": self.models_source,
            "account": {
                "name": cred.get("name"),
                "email": cred.get("email"),
                "subscription": cred.get("subscription"),
                "has_api_key": cred.get("has_api_key"),
                "access_token": cred.get("access_token"),
                "refresh_token": cred.get("refresh_token"),
            } if cred.get("available") else None,
        }


    def models(self) -> list[dict]:
        return [{**m, "tier": _tier(m["name"]), "provider": self.id} for m in self._models]

    def agent_tools(self) -> list[str]:
        # agy has no per-tool allow flag: it is either "edits" (read + write
        # files, commands denied) or everything.
        return ["all tools"] if config.AGENT_ALLOW_SHELL else ["read & edit files"]

    def agent(
        self,
        task: str,
        model: str,
        working_dir: str,
        tools: Optional[list[str]],
        resume: Optional[str],
        effort: Optional[str],
    ) -> AsyncGenerator[dict, None]:
        cmd = self._base(model, self.effort(effort) or self.effort(config.DEFAULT_EFFORT))
        # Without --add-dir agy works in its own scratch folder, not the cwd.
        cmd += ["--add-dir", working_dir]
        cmd += ["--dangerously-skip-permissions"] if config.AGENT_ALLOW_SHELL else ["--mode", "accept-edits"]
        if resume and UUID_RE.match(resume):
            cmd += ["--conversation", resume]
        # agy has no system-prompt flag. Without this note, models tend to
        # reach for the (blocked) shell to list files and then give up.
        note = f"[Workspace: {working_dir}. Work only inside this folder."
        if not config.AGENT_ALLOW_SHELL:
            note += " Shell commands are disabled: use your file tools (list_dir, view_file, write_to_file, replace_file_content) instead."
        messages = [Msg("user", f"{note}]\n\n{task}")]
        return run_once(self, cmd, working_dir, model, messages, config.AGENT_TIMEOUT,
                        self.pool.slots if self.pool else None)
