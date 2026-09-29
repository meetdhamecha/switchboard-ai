"""
switchboard_ai.orchestrator
───────────────────────
Multi-model orchestration on top of the router. Three virtual models:

    auto              Route to one model (router.py). Very hard or
                      multi-part requests escalate to "auto-orchestrate".

    auto-orchestrate  1. PLAN      a strong model picks a strategy:
                                     direct     one routed model answers
                                     decompose  2-5 subtasks for specialists
                                     ensemble   independent answers, merged
                      2. WORK      subtasks run in parallel waves (respecting
                                   dependencies); each is routed to the best
                                   model for its lane; research subtasks get
                                   WebSearch / WebFetch
                      3. REVIEW    a model from another family cross-checks
                                   the findings for errors, contradictions
                                   and gaps
                      4. SYNTHESIZE the final answer is streamed, told about
                                   the review notes

    auto-ensemble     Mixture-of-Agents: diverse proposers (Claude, Gemini,
                      GPT-OSS) answer independently, an aggregator merges.

Every stage fails over to the next candidate model, and appears in the
stream as a tool step (tool_use / tool_result) so clients can show the
pipeline live. Usage is summed over all calls into one result event.

Design follows Anthropic's "Building effective agents" (routing,
parallelization, orchestrator-workers, evaluator-optimizer), the
Mixture-of-Agents paper (Wang et al., ICLR 2025) and routing/cascade
work such as RouteLLM and FrugalGPT.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

from switchboard_ai import config
from switchboard_ai.process import Msg, flatten
from switchboard_ai.providers import registry
from switchboard_ai.providers.base import Provider
from switchboard_ai.router import (
    cool_down, _FABLE, _OPUS, _SONNET, WEB_HINT, Route, _available, _plan, router, with_web_hint,
)

Candidate = tuple[Provider, str, str]   # (provider, model, effort)

_PRO = "antigravity:gemini-3.1-pro-high"
_GPT_OSS = "antigravity:gpt-oss-120b-medium"
_WEB_PROVIDERS = ("claude", "claude-api")   # chat turns with web search / fetch
_WORKER_LANES = ("research", "code", "reasoning", "writing", "creative", "chat")


def _name(p: Provider, model: str) -> str:
    return next((m["name"] for m in p.models() if m["id"] == model), model)


def _context(messages: list[Msg], limit: int) -> str:
    text = flatten(messages)
    return text if len(text) <= limit else "…" + text[-limit:]


def _last_user(messages: list[Msg]) -> str:
    return next((m.content for m in reversed(messages) if m.role == "user"), "")


def _json(text: str) -> Optional[dict]:
    """First JSON object in a model reply (tolerates code fences / prose)."""
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        d = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


def _claude_first(cands: list[Candidate]) -> list[Candidate]:
    """For web work: Claude (CLI or API) has web search / fetch in chat."""
    return sorted(cands, key=lambda c: c[0].id not in _WEB_PROVIDERS)


# ============================================================
# SINGLE ROUTED ANSWER  (used by "auto" and by the "direct" strategy)
# ============================================================

async def run_route(
    route: Route,
    messages: list[Msg],
    session_id: Optional[str],
    effort: Optional[str],
) -> AsyncGenerator[dict, None]:
    """
    Stream one routed answer. A candidate that fails before producing any
    output (rate limit, crash, …) hands over to the next one.
    """
    attempts = route.candidates[:max(1, config.AUTO_MAX_ATTEMPTS)]
    if not attempts:
        yield {"type": "error", "content": registry._none_available()}
        return
    for i, (p, model, planned) in enumerate(attempts):
        if session_id:
            # Another provider's warm process would miss this turn.
            for q in registry.available:
                if q is not p and q.pool:
                    await q.pool.drop(session_id)
        yield route.describe(i)
        web = route.needs_web and p.id in _WEB_PROVIDERS and config.AUTO_WEB_HINT
        held: list[dict] = []
        started, error, code = False, None, None
        async for ev in p.chat(with_web_hint(messages) if web else messages, model, session_id, effort or planned):
            if started:
                yield ev
            elif ev["type"] in ("text", "tool_use"):
                started = True
                for h in held:
                    yield h
                held.clear()
                yield ev
            elif ev["type"] == "error":
                error = ev["content"]
                if ev.get("code") in ("rate_limited", "busy"):
                    code = ev["code"]
            else:
                held.append(ev)
        if error and not started and code == "rate_limited":
            cool_down(p.id, model)      # skip it for a while on later requests
        if started or error is None or i + 1 == len(attempts):
            for h in held:
                yield h
            if error and not started:
                yield {"type": "error", "content": error, **({"code": code} if code else {})}
            return
        yield {"type": "notice", "content": f"{model} failed ({error[:160]}), switched to {attempts[i + 1][1]}."}


# ============================================================
# ONE INTERNAL CALL (collected, with failover)
# ============================================================

@dataclass
class Answer:
    text: str
    provider: Provider
    model: str
    usage: dict = field(default_factory=dict)
    cost: float = 0.0


class _Usage:
    """Token and cost totals over every model call of one orchestrated turn."""

    def __init__(self) -> None:
        self.total = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "thinking_tokens": 0}
        self.cost = 0.0
        self.calls = 0

    def add(self, usage: dict, cost: float = 0.0) -> None:
        self.calls += 1
        self.cost += cost or 0.0
        for k in self.total:
            self.total[k] += (usage or {}).get(k) or 0


async def _ask(
    cands: list[Candidate],
    prompt: str,
    *,
    web: bool = False,
    progress=None,
    tries: int = 2,
) -> Optional[Answer]:
    """Run a one-off prompt on the first candidate that answers."""
    for p, model, effort in cands[:tries]:
        text: list[str] = []
        usage: dict = {}
        cost = 0.0
        body = prompt + (WEB_HINT if web and p.id in _WEB_PROVIDERS else "")

        async def run() -> None:
            nonlocal usage, cost
            async for ev in p.chat([Msg("user", body)], model, None, effort):
                t = ev["type"]
                if t == "text":
                    text.append(ev["content"])
                elif t == "result":
                    usage = ev.get("usage") or {}
                    cost = ev.get("cost_usd") or 0.0
                    if not text and ev.get("content"):
                        text.append(ev["content"])
                elif t == "tool_use" and progress and ev.get("detail"):
                    progress(f"{ev['name']}: {ev['detail']}")

        try:
            await asyncio.wait_for(run(), timeout=config.ORCH_STAGE_TIMEOUT)
        except Exception:
            pass
        out = "".join(text).strip()
        if out:
            return Answer(out, p, model, usage, cost)
    return None


# ============================================================
# PROMPTS
# ============================================================

_PLAN_PROMPT = """You are the planner of a multi-model AI system. Decide how the team should answer the user's latest message. Do not answer it yourself and do not use tools.

Worker lanes: research (can search the web and read URLs), code, reasoning (math, logic, analysis), writing, creative.

Strategies:
- "direct": one strong model can answer well in a single pass. This is right for most requests.
- "decompose": the request has several distinct parts, or needs research AND analysis, or clearly benefits from specialists working separately. Split into 2-{max} self-contained subtasks.
- "ensemble": one hard question where independent answers from different models, cross-checked, make the result more reliable (tricky reasoning, judgment calls, high-stakes facts).

Reply with ONLY this JSON:
{"strategy": "direct|decompose|ensemble", "reason": "one short sentence",
 "lane": "chat|writing|creative|code|reasoning|research", "complexity": 1-5, "needs_web": true|false,
 "subtasks": [{"id": "s1", "title": "3-6 words", "task": "complete, self-contained instruction",
               "lane": "research|code|reasoning|writing|creative", "complexity": 1-5,
               "needs_web": true|false, "depends_on": []}]}
"subtasks" is only for "decompose". Subtasks gather material; they must not write the final answer. Use depends_on only when a subtask truly needs another's output.

<conversation>
{conversation}
</conversation>"""

_WORKER_PROMPT = """You are a specialist on a multi-model team. Do ONLY the subtask below, thoroughly and accurately. Your output goes to a final writer, not to the user: be dense and concrete, state uncertainty, and list the URLs of any sources you used.

The user's request (context only):
<request>
{request}
</request>

Your subtask: {task}{deps}"""

_REVIEW_PROMPT = """You are a strict reviewer. Below are a user's request and the findings of several specialist models. List, briefly, the factual errors, contradictions between findings, unsupported claims, and gaps that matter for a correct and complete final answer. Do not rewrite the answer. If there is nothing important, reply exactly: NO ISSUES

<request>
{request}
</request>

<findings>
{findings}
</findings>"""

_SYNTH_PROMPT = """{conversation}

---
[Internal notes from your research team: the user cannot see them.]
<findings>
{findings}
</findings>
{review}
Using these notes (they may contain mistakes; the review flags known problems), write the best possible reply to the user's latest message. Reply directly to the user. Do not mention the team, subtasks, reviewers or this pipeline. Keep links to sources that support key claims."""

_MOA_PROMPT = """{conversation}

---
[Internal: several AI models independently answered the user's latest message. The user cannot see these answers.]
{proposals}

Synthesize them into one reply that is better than any single answer. Critically evaluate them: some may be biased, outdated or wrong. Keep what is correct and well supported, resolve disagreements (say so if it is genuinely uncertain), and add anything important they all missed. Reply directly to the user and do not mention the other models."""


# ============================================================
# ORCHESTRATOR
# ============================================================

class Orchestrator:
    # ── public entry ────────────────────────────────────────

    async def stream(
        self,
        mode: str,
        messages: list[Msg],
        session_id: Optional[str],
        effort: Optional[str],
    ) -> AsyncGenerator[dict, None]:
        if mode == "auto":
            route = await router.route(messages, session_id)
            if not route.escalate:
                async for ev in run_route(route, messages, session_id, effort):
                    yield ev
                return
            yield {"type": "notice", "content": "Hard or multi-part request: using the multi-model orchestrator."}
            mode = "orchestrate"

        # Orchestrated turns are stateless. Any warm session would miss this
        # turn, so drop it: the next turn starts fresh with the full transcript.
        if session_id:
            for p in registry.available:
                if p.pool:
                    await p.pool.drop(session_id)

        run = self._orchestrate(messages, effort) if mode == "orchestrate" else self._ensemble(messages, effort, None)
        async for ev in run:
            yield ev

    # ── plumbing ────────────────────────────────────────────

    @staticmethod
    def _step(sid: str, name: str, detail: str = "") -> dict:
        return {"type": "tool_use", "id": sid, "name": name, "detail": detail}

    @staticmethod
    def _done(sid: str, ok: bool, err: str = "") -> dict:
        return {"type": "tool_result", "id": sid, "ok": ok, "content": err}

    async def _synthesize(
        self,
        prompt: str,
        cands: list[Candidate],
        lane: str,
        complexity: int,
        reason: str,
        effort: Optional[str],
        usage: _Usage,
    ) -> AsyncGenerator[dict, None]:
        """Stream the final answer; replace per-call results with one total."""
        route = Route(lane, complexity, False, "orchestrator", reason, cands)
        final: dict = {}
        async for ev in run_route(route, [Msg("user", prompt)], None, effort):
            if ev["type"] == "result":
                final = ev
                usage.add(ev.get("usage") or {}, ev.get("cost_usd") or 0.0)
            else:
                yield ev
        yield {
            **final,
            "type": "result",
            "usage": dict(usage.total),
            "cost_usd": round(usage.cost, 6),
            "orchestration": {"calls": usage.calls},
        }

    # ── planner → workers → review → synthesis ─────────────

    async def _orchestrate(self, messages: list[Msg], effort: Optional[str]) -> AsyncGenerator[dict, None]:
        usage = _Usage()
        conversation = _context(messages, 30_000)

        yield self._step("orch-plan", "Plan", "choosing a strategy")
        planners = _available([(_OPUS, "medium"), (_SONNET, "high"), (_PRO, "high")])
        ans = await _ask(planners, _PLAN_PROMPT.replace("{max}", str(config.ORCH_MAX_SUBTASKS))
                         .replace("{conversation}", conversation))
        plan = _json(ans.text) if ans else None
        if ans:
            usage.add(ans.usage, ans.cost)
        if not plan:
            yield self._done("orch-plan", False, "planner unavailable; answering directly")
            plan = {"strategy": "direct"}

        strategy = str(plan.get("strategy", "direct")).lower()
        lane = str(plan.get("lane", "reasoning")).lower()
        lane = lane if lane in _WORKER_LANES else "reasoning"
        try:
            c = max(1, min(5, int(plan.get("complexity", 4))))
        except (TypeError, ValueError):
            c = 4
        subtasks = [s for s in plan.get("subtasks") or [] if isinstance(s, dict) and s.get("task")]
        subtasks = subtasks[:config.ORCH_MAX_SUBTASKS]
        if strategy == "decompose" and len(subtasks) < 2:
            strategy = "direct"
        reason = str(plan.get("reason", "")).strip()
        if ans:
            detail = {"direct": "one expert model", "ensemble": "independent answers, merged",
                      "decompose": f"{len(subtasks)} parallel specialists"}.get(strategy, strategy)
            yield self._step("orch-plan", "Plan", f"{detail} · {_name(ans.provider, ans.model)}")
            yield self._done("orch-plan", True)

        if strategy == "ensemble":
            async for ev in self._ensemble(messages, effort, usage):
                yield ev
            return

        if strategy != "decompose":
            route = await router.route(messages)
            if plan.get("lane"):   # the planner's read of the request wins
                c = max(c, route.complexity)
                route = Route(lane, c, bool(plan.get("needs_web")) or route.needs_web,
                              "orchestrator", reason or route.reason, _available(_plan(lane, c)))
            async for ev in self._synthesize_direct(route, messages, effort, usage):
                yield ev
            return

        # ── workers, in dependency waves ──
        request = _last_user(messages)[-8000:]
        results: dict[str, Answer] = {}
        titles = {str(s.get("id") or f"s{i + 1}"): s for i, s in enumerate(subtasks)}
        pending = dict(titles)
        while pending:
            ready = {k: s for k, s in pending.items()
                     if all(str(d) in results or str(d) not in titles for d in (s.get("depends_on") or []))}
            if not ready:          # a dependency cycle or a failed dependency: run the rest anyway
                ready = dict(pending)
            async for ev in self._run_wave(ready, results, request, usage):
                yield ev
            for k in ready:
                pending.pop(k, None)

        if not results:
            yield {"type": "notice", "content": "No specialist answered; falling back to a single model."}
            async for ev in self._synthesize_direct(await router.route(messages), messages, effort, usage):
                yield ev
            return

        findings = "\n\n".join(
            f"### {titles[k].get('title') or k} ({_name(a.provider, a.model)})\n{a.text}"
            for k, a in results.items()
        )

        # ── review by a different model family ──
        review = ""
        if config.ORCH_REVIEW and (len(results) >= 2 or c >= 4):
            yield self._step("orch-review", "Review", "cross-checking findings")
            reviewers = _available([(_PRO, "high"), (_SONNET, "high"), (_OPUS, "medium")])
            rv = await _ask(reviewers, _REVIEW_PROMPT.replace("{request}", request)
                            .replace("{findings}", findings[-60_000:]))
            if rv:
                usage.add(rv.usage, rv.cost)
                clean = "NO ISSUES" in rv.text.upper()[:40]
                review = "" if clean else f"<review>\n{rv.text}\n</review>\n"
                yield self._step("orch-review", "Review",
                                 f"{'no issues' if clean else 'issues flagged'} · {_name(rv.provider, rv.model)}")
                yield self._done("orch-review", True)
            else:
                yield self._done("orch-review", False, "reviewer unavailable")

        # ── synthesis ──
        synth = _available([(_OPUS, "high" if c >= 4 else "medium"), (_PRO, "high"), (_SONNET, "high")])
        prompt = (_SYNTH_PROMPT.replace("{conversation}", _context(messages, 40_000))
                  .replace("{findings}", findings[-80_000:]).replace("{review}", review))
        async for ev in self._synthesize(prompt, synth, "orchestrated", c,
                                         reason or f"{len(results)} specialists", effort, usage):
            yield ev

    async def _synthesize_direct(
        self, route: Route, messages: list[Msg], effort: Optional[str], usage: _Usage,
    ) -> AsyncGenerator[dict, None]:
        async for ev in run_route(route, messages, None, effort):
            if ev["type"] == "result":
                usage.add(ev.get("usage") or {}, ev.get("cost_usd") or 0.0)
                yield {**ev, "usage": dict(usage.total), "cost_usd": round(usage.cost, 6),
                       "orchestration": {"calls": usage.calls}}
            else:
                yield ev

    async def _run_wave(
        self, wave: dict[str, dict], results: dict[str, Answer], request: str, usage: _Usage,
    ) -> AsyncGenerator[dict, None]:
        q: asyncio.Queue = asyncio.Queue()

        async def work(key: str, s: dict) -> None:
            sid = f"orch-{key}"
            lane = str(s.get("lane", "reasoning")).lower()
            lane = lane if lane in _WORKER_LANES else "reasoning"
            try:
                c = max(1, min(5, int(s.get("complexity", 3))))
            except (TypeError, ValueError):
                c = 3
            web = bool(s.get("needs_web")) or lane == "research"
            cands = _available(_plan(lane, c))
            if web:
                cands = _claude_first(cands)
            title = str(s.get("title") or key)
            label = f"{lane.title()} · {_name(cands[0][0], cands[0][1])}" if cands else lane.title()
            await q.put(self._step(sid, label, title))

            deps = [results[str(d)] for d in (s.get("depends_on") or []) if str(d) in results]
            dep_text = "".join(f"\n\nInput from an earlier step:\n{a.text[-12_000:]}" for a in deps)
            prompt = (_WORKER_PROMPT.replace("{request}", request).replace("{task}", str(s["task"]))
                      .replace("{deps}", dep_text))

            def progress(detail: str) -> None:
                q.put_nowait(self._step(sid, label, f"{title} · {detail}"))

            ans = await _ask(cands, prompt, web=web, progress=progress)
            if ans:
                usage.add(ans.usage, ans.cost)
                results[key] = ans
                await q.put(self._step(sid, f"{lane.title()} · {_name(ans.provider, ans.model)}", title))
                await q.put(self._done(sid, True))
            else:
                await q.put(self._done(sid, False, "no model could complete this subtask"))

        tasks = [asyncio.create_task(work(k, s)) for k, s in wave.items()]
        done = asyncio.gather(*tasks, return_exceptions=True)
        try:
            while not (done.done() and q.empty()):
                try:
                    yield await asyncio.wait_for(q.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in tasks:
                t.cancel()

    # ── mixture of agents ──────────────────────────────────

    async def _ensemble(
        self, messages: list[Msg], effort: Optional[str], usage: Optional[_Usage],
    ) -> AsyncGenerator[dict, None]:
        usage = usage or _Usage()
        route = await router.route(messages)
        web = route.needs_web

        # Diversity matters more than count: one model per family.
        families = [
            [(_OPUS, "high"), (_SONNET, "high")],
            [(_PRO, "high"), ("antigravity:gemini-3.8-flash-high", "high")],
            [(_GPT_OSS, "medium"), ("antigravity:claude-opus-4-6-thinking", "high"), (_FABLE, "medium")],
        ]
        proposers: list[list[Candidate]] = []
        used: set[tuple[str, str]] = set()
        for fam in families:
            cands = [c for c in _available(fam) if (c[0].id, c[1]) not in used][:2]
            if cands:
                used.add((cands[0][0].id, cands[0][1]))
                proposers.append(cands)
        if len(proposers) < 2:
            yield {"type": "notice", "content": "Not enough different models for an ensemble; answering directly."}
            async for ev in self._synthesize_direct(route, messages, effort, usage):
                yield ev
            return

        conversation = _context(messages, 30_000)
        answers: dict[int, Answer] = {}
        q: asyncio.Queue = asyncio.Queue()

        async def propose(i: int, cands: list[Candidate]) -> None:
            sid = f"orch-p{i}"
            label = f"Proposal · {_name(cands[0][0], cands[0][1])}"
            await q.put(self._step(sid, label, "independent answer"))

            def progress(detail: str) -> None:
                q.put_nowait(self._step(sid, label, detail))

            ans = await _ask(cands, conversation, web=web and cands[0][0].id in _WEB_PROVIDERS, progress=progress)
            if ans:
                usage.add(ans.usage, ans.cost)
                answers[i] = ans
                await q.put(self._step(sid, f"Proposal · {_name(ans.provider, ans.model)}", "independent answer"))
                await q.put(self._done(sid, True))
            else:
                await q.put(self._done(sid, False, "no answer"))

        tasks = [asyncio.create_task(propose(i, c)) for i, c in enumerate(proposers)]
        done = asyncio.gather(*tasks, return_exceptions=True)
        try:
            while not (done.done() and q.empty()):
                try:
                    yield await asyncio.wait_for(q.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in tasks:
                t.cancel()

        if not answers:
            async for ev in self._synthesize_direct(route, messages, effort, usage):
                yield ev
            return

        proposals = "\n\n".join(
            f"<answer model=\"{_name(a.provider, a.model)}\">\n{a.text[-20_000:]}\n</answer>"
            for a in answers.values()
        )
        prompt = _MOA_PROMPT.replace("{conversation}", _context(messages, 40_000)).replace("{proposals}", proposals)
        aggregators = _available([(_OPUS, "high"), (_PRO, "high"), (_SONNET, "high")])
        async for ev in self._synthesize(prompt, aggregators, "ensemble", max(route.complexity, 4),
                                         f"mixture of {len(answers)} models", effort, usage):
            yield ev


orchestrator = Orchestrator()
