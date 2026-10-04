"""
switchboard_ai.process
──────────────────
Long-lived CLI processes that take one user message per line on stdin.

Both binaries support this mode (`--input-format stream-json`):

    claude.exe  cold start ~2 s   → warm turn ~1.2 s
    agy.exe     cold start ~12 s  → warm turn ~2 s

So instead of spawning a process per request we keep:

  • one process per conversation (session_id) — context lives in the
    process, so each turn only sends the NEW user message, and the prompt
    cache stays hot;
  • "spare" processes pre-spawned for the most recently used models, so
    even a brand-new conversation or a stateless API call starts warm;
  • a concurrency limit (Slots) with a wait queue, so a burst of requests
    can't start more processes than the machine can run.

A provider plugs in by supplying a Driver (command line, message encoding,
output parser). Everything else here is shared.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncGenerator, Callable, Optional, Protocol

# stream-json lines carrying tool results (e.g. a whole file that was Read)
# easily exceed asyncio's default 64 KB line limit.
_LINE_LIMIT = 64 * 1024 * 1024
_REAP_INTERVAL = 60


@dataclass
class Msg:
    role: str
    content: str


def flatten(messages: list[Msg]) -> str:
    """Turn a transcript into one prompt for a process that has no history."""
    if len(messages) == 1 and messages[0].role == "user":
        return messages[0].content
    system = [m.content for m in messages if m.role == "system"]
    turns = [m for m in messages if m.role != "system"]
    parts: list[str] = []
    if system:
        parts.append("Instructions:\n" + "\n\n".join(system))
    if len(turns) > 1:
        parts.append("Conversation so far:")
    for m in turns:
        label = {"user": "Human", "assistant": "Assistant"}.get(m.role, m.role.title())
        parts.append(f"{label}: {m.content}")
    return "\n\n".join(parts)


def last_user(messages: list[Msg]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    return messages[-1].content if messages else ""


class Driver(Protocol):
    """What a provider must supply to run in this module."""

    binary: str

    def encode(self, text: str) -> bytes: ...

    def new_parser(self) -> Callable[[str], list[dict]]: ...


class ProcessSession:
    """One live process. Turns are serialized: the CLI handles one at a time."""

    def __init__(
        self,
        driver: Driver,
        command: list[str],
        cwd: str,
        model: str,
        effort: str = "",
        timeout: int = 300,
    ):
        self.driver = driver
        self.command = command
        self.cwd = cwd
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.session_id = ""
        self.native_id = ""  # conversation id reported by the binary
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.lock = asyncio.Lock()
        self.turns = 0
        self.created = time.monotonic()
        self.last_used = self.created
        self._stderr_tail = b""
        self._stderr_task: Optional[asyncio.Task] = None

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        if self.is_alive:
            return
        extra_kwargs = {}
        if os.name == "nt":
            extra_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            limit=_LINE_LIMIT,
            **extra_kwargs,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr(self.proc))
        self.last_used = time.monotonic()

    async def _drain_stderr(self, proc) -> None:
        # Always drain stderr, or a chatty process blocks on a full pipe.
        try:
            while chunk := await proc.stderr.read(4096):
                self._stderr_tail = (self._stderr_tail + chunk)[-4000:]
        except Exception:
            pass

    def stderr_text(self) -> str:
        return self._stderr_tail.decode("utf-8", errors="replace").strip()

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        if self._stderr_task:
            self._stderr_task.cancel()

    async def ask(self, messages: list[Msg]) -> AsyncGenerator[dict, None]:
        """
        Run one turn. The first turn of a process gets the whole transcript;
        later turns only the newest user message, since the process already
        holds the earlier ones.
        """
        async with self.lock:
            if not self.is_alive:
                # Died between turns: a fresh process has no history.
                await self.close()
                self.turns = 0
                try:
                    await self.start()
                except Exception as e:
                    yield {"type": "error", "content": f"could not start {self.driver.binary}: {e}"}
                    return

            text = last_user(messages) if self.turns > 0 else flatten(messages)
            proc = self.proc
            try:
                proc.stdin.write(self.driver.encode(text))
                await proc.stdin.drain()
            except Exception as e:
                await self.close()
                yield {"type": "error", "content": f"process write failed: {e}"}
                return

            parse = self.driver.new_parser()
            deadline = time.monotonic() + self.timeout
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        await self.close()
                        yield {"type": "error", "content": f"turn timed out after {self.timeout}s"}
                        return
                    try:
                        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                    except asyncio.TimeoutError:
                        continue

                    if not raw:  # EOF: the process exited mid-turn
                        await asyncio.sleep(0.2)  # let stderr drain
                        await self.close()
                        detail = self.stderr_text()
                        yield {
                            "type": "error",
                            "content": detail or "the model process exited before finishing",
                        }
                        return

                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    done = False
                    for ev in parse(line):
                        if ev["type"] == "result":
                            done = True
                            self.native_id = ev.get("native_session_id") or self.native_id
                        yield ev
                    if done:
                        self.turns += 1
                        self.last_used = time.monotonic()
                        return
            except (asyncio.CancelledError, GeneratorExit):
                # Client went away mid-turn: output position is now unknown,
                # so the process cannot be reused.
                await self.close()
                raise


CommandFactory = Callable[[str, str], list[str]]  # (model, effort) -> argv


class Busy(Exception):
    """No free slot within the queue timeout."""


class Slots:
    """
    Bounded concurrency with a wait queue. Every turn holds a slot while its
    process works, so the number of busy CLI processes (and their CPU/RAM)
    stays bounded no matter how many requests arrive at once.
    """

    def __init__(self, limit: int, queue_timeout: float):
        self.limit = max(1, limit)
        self.queue_timeout = queue_timeout
        self._sem = asyncio.Semaphore(self.limit)
        self.active = 0
        self.waiting = 0

    @asynccontextmanager
    async def hold(self):
        self.waiting += 1
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self.queue_timeout)
        except asyncio.TimeoutError:
            raise Busy(
                f"server busy: {self.active} requests running, {self.waiting - 1} queued; "
                f"no slot freed within {self.queue_timeout:.0f}s"
            ) from None
        finally:
            self.waiting -= 1
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._sem.release()

    def stats(self) -> dict:
        return {"limit": self.limit, "active": self.active, "queued": self.waiting}


def busy_event(e: Busy) -> dict:
    return {"type": "error", "code": "busy", "content": str(e)}


_SPARE_MAX_AGE = 30 * 60  # recycle spares so their system prompt (date) stays fresh


class SessionPool:
    """
    session_id -> warm ProcessSession, plus pre-spawned spares, behind a
    concurrency limit. One pool per provider.
    """

    def __init__(
        self,
        driver: Driver,
        command: CommandFactory,
        cwd: str,
        spares: int,
        idle_ttl: int,
        timeout: int,
        max_concurrent: int = 6,
        queue_timeout: float = 90,
        max_sessions: int = 12,
        spare_max_total: int = 3,
    ):
        self.driver = driver
        self.command = command
        self.cwd = cwd
        self.spare_target = max(0, spares)
        self.spare_max_total = max(self.spare_target, spare_max_total)
        self.idle_ttl = idle_ttl
        self.timeout = timeout
        self.max_sessions = max(1, max_sessions)
        self.slots = Slots(max_concurrent, queue_timeout)
        self._sessions: dict[str, ProcessSession] = {}
        self._spares: list[ProcessSession] = []
        self._key_used: dict[tuple[str, str], float] = {}   # (model, effort) -> last use
        self._guard = asyncio.Lock()
        self._spare_lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()
        self._reaper: Optional[asyncio.Task] = None
        self._closed = False

    def _new(self, model: str, effort: str) -> ProcessSession:
        return ProcessSession(
            self.driver, self.command(model, effort), self.cwd, model, effort, self.timeout
        )

    # ── spares ──────────────────────────────────────────────
    # Kept per (model, effort) for the most recently used keys, so switching
    # between a few models (as "auto" does) still starts warm.

    def _take_spare(self, model: str, effort: str) -> Optional[ProcessSession]:
        for s in list(self._spares):
            if s.model == model and s.effort == effort:
                self._spares.remove(s)
                if s.is_alive:
                    return s
                self._spawn_bg(s.close())
        return None

    def _spawn_bg(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _refill(self, model: str, effort: str) -> None:
        if not self.spare_target or self._closed:
            return
        key = (model, effort)
        async with self._spare_lock:
            self._spares = [s for s in self._spares if s.is_alive]
            if sum((s.model, s.effort) == key for s in self._spares) >= self.spare_target:
                return
            while len(self._spares) >= self.spare_max_total:
                # Evict a spare of the least recently used model.
                victim = min(self._spares, key=lambda s: self._key_used.get((s.model, s.effort), 0))
                self._spares.remove(victim)
                await victim.close()
            s = self._new(model, effort)
            try:
                await s.start()
            except Exception:
                return
            if self._closed:  # shut down while we were spawning
                await s.close()
            else:
                self._spares.append(s)

    async def warmup(self, model: str, effort: str = "") -> None:
        self._key_used[(model, effort)] = time.monotonic()
        await self._refill(model, effort)

    async def _acquire(self, model: str, effort: str) -> ProcessSession:
        self._key_used[(model, effort)] = time.monotonic()
        sess = self._take_spare(model, effort) or self._new(model, effort)
        self._spawn_bg(self._refill(model, effort))
        return sess

    def _evict_lru_session(self) -> None:
        """Called under _guard: make room for one more warm session."""
        if len(self._sessions) < self.max_sessions:
            return
        idle = [s for s in self._sessions.values() if not s.lock.locked()]
        if not idle:
            return
        victim = min(idle, key=lambda s: s.last_used)
        self._sessions.pop(victim.session_id, None)
        self._spawn_bg(victim.close())

    # ── turns ───────────────────────────────────────────────

    async def ask(
        self,
        session_id: Optional[str],
        messages: list[Msg],
        model: str,
        effort: str = "",
    ) -> AsyncGenerator[dict, None]:
        try:
            async with self.slots.hold():
                async for ev in self._ask(session_id, messages, model, effort):
                    yield ev
        except Busy as e:
            yield busy_event(e)

    async def _ask(
        self,
        session_id: Optional[str],
        messages: list[Msg],
        model: str,
        effort: str,
    ) -> AsyncGenerator[dict, None]:
        if not session_id:
            # Stateless call: borrow a warm process for one turn.
            sess = await self._acquire(model, effort)
            try:
                async for ev in sess.ask(messages):
                    yield ev
            finally:
                await sess.close()
            return

        async with self._guard:
            sess = self._sessions.get(session_id)
            if sess is not None and (sess.model, sess.effort) != (model, effort):
                # Model/effort are fixed at spawn. The replacement starts at
                # turn 0, so it receives the full transcript.
                self._sessions.pop(session_id, None)
                self._spawn_bg(sess.close())
                sess = None
            if sess is None:
                self._evict_lru_session()
                sess = await self._acquire(model, effort)
                sess.session_id = session_id
                self._sessions[session_id] = sess
            else:
                self._key_used[(model, effort)] = time.monotonic()

        async for ev in sess.ask(messages):
            yield ev

    async def drop(self, session_id: str) -> bool:
        async with self._guard:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return False
        await sess.close()
        return True

    async def close_all(self) -> None:
        self._closed = True
        async with self._spare_lock:  # wait out an in-flight refill
            pass
        async with self._guard:
            victims = list(self._sessions.values()) + self._spares
            self._sessions.clear()
            self._spares = []
        for s in victims:
            await s.close()

    def reopen(self) -> None:
        """Allow warm spares again after close_all (e.g. after an account switch)."""
        self._closed = False

    def stats(self) -> dict:
        now = time.monotonic()
        return {
            "concurrency": self.slots.stats(),
            "active_sessions": len(self._sessions),
            "max_sessions": self.max_sessions,
            "warm_spares": sum(1 for s in self._spares if s.is_alive),
            "spares": [s.model + ("/" + s.effort if s.effort else "") for s in self._spares if s.is_alive],
            "idle_ttl_seconds": self.idle_ttl,
            "sessions": [
                {
                    "id": s.session_id,
                    "model": s.model,
                    "alive": s.is_alive,
                    "busy": s.lock.locked(),
                    "turns": s.turns,
                    "idle_seconds": round(now - s.last_used, 1),
                }
                for s in self._sessions.values()
            ],
        }

    # ── idle reaper ─────────────────────────────────────────

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(_REAP_INTERVAL)
            now = time.monotonic()
            async with self._guard:
                stale = [
                    sid for sid, s in self._sessions.items()
                    if now - s.last_used > self.idle_ttl and not s.lock.locked()
                ]
                victims = [self._sessions.pop(sid) for sid in stale]
            async with self._spare_lock:
                old = [s for s in self._spares if now - s.created > _SPARE_MAX_AGE or not s.is_alive]
                self._spares = [s for s in self._spares if s not in old]
            for s in victims:
                await s.close()
            for s in old:
                # Replace an aged spare before closing it, so a model that was
                # warm stays warm. Dropping it made the next request pay the
                # full cold start (agy: ~10 s). Dead ones are not respawned.
                if s.is_alive:
                    await self._refill(s.model, s.effort)
                await s.close()

    def start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())

    async def stop_reaper(self) -> None:
        if self._reaper and not self._reaper.done():
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
        self._reaper = None


async def run_once(
    driver: Driver,
    command: list[str],
    cwd: str,
    model: str,
    messages: list[Msg],
    timeout: int,
    slots: Optional[Slots] = None,
) -> AsyncGenerator[dict, None]:
    """Spawn a dedicated process for one turn (agent tasks), then close it."""
    sess = ProcessSession(driver, command, cwd, model, timeout=timeout)
    sess.session_id = f"once_{uuid.uuid4().hex[:8]}"
    try:
        if slots is None:
            async for ev in sess.ask(messages):
                yield ev
            return
        async with slots.hold():
            async for ev in sess.ask(messages):
                yield ev
    except Busy as e:
        yield busy_event(e)
    finally:
        await sess.close()
