"""
switchboard_ai.router
─────────────────
The "auto" model: picks a model, effort and tools for each request.

    1. Classify the request into a lane plus a complexity (1-5):

           chat       small talk, simple questions
           writing    emails, summaries, rewrites, translation
           creative   stories, poems, brainstorming, naming
           code       programming, debugging
           reasoning  math, logic, analysis, planning
           research   needs current / external information (web)
           long       input too large for the usual models

       Cheap pattern signals decide the clear cases instantly. Unclear ones
       go to a small, fast LLM classifier (AUTO_CLASSIFIER=hybrid, default).

    2. Map lane + complexity to a ranked list of (provider, model, effort)
       candidates. Complexity drives both the model tier and the effort, so
       easy prompts stay fast and hard ones get deep thinking.

    3. The server runs the first candidate and falls through to the next one
       if it fails before producing any output (rate limit, crash, …).

Short follow-ups ("why?", "make it shorter") are classified together with
the previous question, and a session never drops to a weaker model within
the same lane — that would lose quality and respawn the warm process.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from switchboard_ai import config
from switchboard_ai.process import Msg
from switchboard_ai.providers import registry
from switchboard_ai.providers.base import Provider
from switchboard_ai.providers.claude_api import CLI_TO_API

AUTO_ID = "auto"

LANES = ("chat", "writing", "creative", "code", "reasoning", "research", "long")

# Above this many characters of transcript, prefer the long-context lane.
LONG_CONTEXT_CHARS = 500_000

_CLAUDE_EFFORT = {1: "low", 2: "low", 3: "medium", 4: "high", 5: "xhigh"}
_LEVEL = {1: "low", 2: "low", 3: "medium", 4: "high", 5: "high"}  # agy variants

_HAIKU = "claude:claude-haiku-4-5-20251001"
_SONNET = "claude:claude-sonnet-5"
_OPUS = "claude:claude-opus-5-5"
_FABLE = "claude:claude-fable-5-1"


# Virtual models → orchestration mode (see orchestrator.py).
MODES = {
    "auto": "auto",                          # route; escalate hard requests
    "auto-orchestrate": "orchestrate",       # planner → workers → review → synthesis
    "auto-ensemble": "ensemble",             # mixture-of-agents
}


def mode_of(model: Optional[str]) -> Optional[str]:
    m = (model or "").strip().lower()
    for prefix in ("auto:", "switchboard:"):
        if m.startswith(prefix) and m[len(prefix):] in MODES:
            m = m[len(prefix):]
    return MODES.get(m)


def is_auto(model: Optional[str]) -> bool:
    return mode_of(model) is not None


# ============================================================
# CANDIDATES
# ============================================================

def _flash(c: int) -> str:
    return f"antigravity:gemini-3.8-flash-{_LEVEL[c]}"


def _plan(lane: str, c: int) -> list[tuple[str, str]]:
    """Ranked ("provider:model", effort) choices for a lane and complexity."""
    ce, lv = _CLAUDE_EFFORT[c], _LEVEL[c]
    pro = ("antigravity:gemini-3.1-pro-high", "high")
    opus_thinking = ("antigravity:claude-opus-4-6-thinking", "high")

    if lane == "chat":
        first = [(_HAIKU, "low")] if c <= 2 else [(_SONNET, ce)]
        return first + [(_flash(c), lv), (_SONNET, ce)]
    if lane == "writing":
        first = [(_OPUS, "medium")] if c >= 5 else [(_SONNET, ce)]
        return first + [(_flash(c), lv), ("antigravity:gemini-3.7-flash-" + lv, lv)]
    if lane == "creative":
        e = "high" if c >= 4 else "medium"
        return [(_FABLE, e), ("claude:claude-fable-5", e), ("antigravity:gemini-3.8-flash-high", "high")]
    if lane == "code":
        if c >= 4:
            return [(_OPUS, ce), (_SONNET, "high"), pro, opus_thinking]
        return [(_SONNET, ce), (_flash(c), lv), pro]
    if lane == "reasoning":
        if c >= 4:
            return [(_OPUS, ce), pro, opus_thinking, ("antigravity:gpt-oss-120b-medium", "medium")]
        if c == 3:
            return [(_SONNET, "high"), pro, (_OPUS, "medium")]
        return [(_SONNET, "medium"), (_flash(c), lv)]
    if lane == "research":
        # Claude first: its chat mode has WebSearch / WebFetch pre-approved.
        if c >= 4:
            return [(_OPUS, ce), (_SONNET, "high"), ("antigravity:gemini-3.8-flash-high", "high")]
        return [(_SONNET, "medium"), (_OPUS, "medium"), ("antigravity:gemini-3.8-flash-high", "high")]
    if lane == "long":
        return [pro, (_OPUS, "high"), ("antigravity:gemini-3.8-flash-high", "high")]
    return [(_SONNET, ce)]


# (provider, model) → time until which it is skipped after a usage-limit /
# rate-limit error, so every request doesn't pay for the same failed attempt.
_COOLDOWN_SECONDS = 600
_cooldown: dict[tuple[str, str], float] = {}


def cool_down(provider_id: str, model: str) -> None:
    _cooldown[(provider_id, model)] = time.monotonic() + _COOLDOWN_SECONDS


def _cooling(provider_id: str, model: str) -> bool:
    until = _cooldown.get((provider_id, model))
    if until and until > time.monotonic():
        return True
    _cooldown.pop((provider_id, model), None)
    return False


def _available(plan: list[tuple[str, str]]) -> list[tuple[Provider, str, str]]:
    out: list[tuple[Provider, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, effort in plan:
        pid, model = key.split(":", 1)
        if pid == "claude" and registry.api_preferred:
            # Same model over the official API: parallel-safe for many users.
            pid, model = "claude-api", CLI_TO_API.get(model, model)
        p = registry.get(pid)
        if not p or not p.available or not any(m["id"] == model for m in p.models()):
            continue
        if _cooling(p.id, model):
            continue
        if (p.id, model) not in seen:
            seen.add((p.id, model))
            out.append((p, *p.resolve_effort(model, effort)))
    # Last resort: the server default, then anything that is up.
    dp, dm = registry.default()
    if dp and (dp.id, dm) not in seen:
        out.append((dp, dm, ""))
    return out


# ============================================================
# HEURISTIC CLASSIFIER
# ============================================================

_I, _M = re.IGNORECASE, re.MULTILINE

_SMALLTALK = re.compile(
    r"^\W*(hi+|hello|hey+|yo|thanks?( you)?( so much)?|thx|ok(ay)?|cool|nice|great|awesome|"
    r"good (morning|afternoon|evening|night)|bye|see you|how are you|who are you|"
    r"what('?s| is) your name)\W*$", _I)
_URL = re.compile(r"https?://\S+", _I)
_SEARCH = re.compile(
    r"\b(search (the web|online|for)|web ?search|look (it |this |that )?up|google (it|this|for)|browse|"
    r"find (me )?(sources|links|articles|papers)|with (sources|citations|links)|cite (your )?sources|"
    r"(deep |online )?research)\b", _I)
_FRESH = re.compile(
    r"\b(latest|newest|today|tonight|yesterday|this (week|month|year)|right now|at the moment|"
    r"news|headlines?|breaking|recent(ly)?|trending|up[- ]to[- ]date|as of now|"
    r"current (events|price|version|status|ceo|president|prime minister|state of)|"
    r"price of|stock price|share price|exchange rate|weather|forecast|live score|"
    r"release (date|notes)|just (released|announced)|20(2[6-9]|3\d))\b", _I)
_CODE_BLOCK = re.compile(r"```", _M)
_TRACE = re.compile(
    r"Traceback \(most recent call last\)|\b\w+(Error|Exception)\b\s*[:(]|line \d+, in |"
    r"\bat \S+ \(\S+:\d+:\d+\)|segmentation fault|stack ?trace|npm ERR!|cannot find module|"
    r"undefined is not|null pointer|exit code \d+", _I)
_CODE_SYNTAX = re.compile(
    r"^\s*(def |class |import |from \S+ import |function |const |let |var |public |private |"
    r"#include|SELECT |INSERT |CREATE TABLE|async def |fn |func |package )|=>|console\.log|"
    r"\w+\([^)]*\)\s*\{|</?[a-z][\w-]*[^>]*>", _M | _I)
_CODE_WORDS = re.compile(
    r"\b(python|javascript|typescript|java|c\+\+|c#|golang|rust|kotlin|swift|php|ruby|sql|regex|"
    r"html|css|react|vue|angular|next\.?js|fastapi|django|flask|node(\.?js)?|express|api|endpoint|"
    r"function|method|class|script|code|coding|compile|refactor|unit tests?|debug(ging)?|bug|"
    r"algorithm|data structure|docker|kubernetes|git|bash|powershell|json|yaml|database|backend|"
    r"frontend|repo(sitory)?|library|framework|async|lambda|sdk)\b", _I)
_MATH = re.compile(
    r"\b(prove|proof|derive|derivation|theorem|lemma|integral|integrate|differentiate|derivative|"
    r"equation|solve for|probability|statistic(s|al)|matrix|matrices|eigen\w*|vector space|"
    r"combinatorics|permutations?|modulo|calculus|algebra|geometry|physics|formula|big-?o)\b|"
    r"[∫∑∏√π≤≥≠∞∂]|\d\s*[\^*/]\s*\d|\bx\s*[=+\-]\s*\d", _I)
_PROOF = re.compile(r"\b(prove|proof|theorem|lemma|derive|rigorous)\b", _I)
_REASON = re.compile(
    r"\b(step[- ]by[- ]step|analy[sz]e|analysis|compare|comparison|trade-?offs?|pros and cons|"
    r"design|architect(ure)?|strategy|strategic|plan(ning)?|evaluate|critique|reason(ing)?|"
    r"why (does|is|do|are|did)|explain (why|how)|root cause|implications?|in[- ]depth|"
    r"deep dive|comprehensive|thorough(ly)?|detailed|framework for|decide|decision)\b", _I)
_CREATIVE = re.compile(
    r"\b(poem|poetry|haiku|limerick|story|stories|short story|fiction|novel|fairy ?tale|lyrics|"
    r"song|screenplay|dialogue|joke|slogan|tagline|brainstorm|names? for|name ideas|creative(ly)?|"
    r"imagine|fantasy|character|plot|rap|rhyme|metaphor|world-?building)\b", _I)
_WRITING = re.compile(
    r"\b(e-?mail|letter|essay|blog( post)?|article|summari[sz]e|summary|tl;?dr|rewrite|rephrase|"
    r"paraphrase|proofread|grammar|translate|translation|tone|cover letter|resume|cv|linkedin|"
    r"tweet|caption|outline|report|memo|announcement|description|bio)\b", _I)
_DEEP = re.compile(
    r"\b(think (hard|carefully|deeply|step by step)|ultrathink|rigorous(ly)?|very (hard|difficult|"
    r"complex)|hardest|as detailed as|leave nothing out|production[- ]ready|end[- ]to[- ]end|"
    r"from scratch|full (app|system|implementation))\b", _I)
_QUICK = re.compile(
    r"\b(quick(ly)?|brief(ly)?|short answer|one (line|word|sentence)|in short|just tell|"
    r"simple answer|yes or no)\b", _I)


def _hits(rx: re.Pattern, text: str, cap: int) -> int:
    return min(cap, len(rx.findall(text)))


@dataclass
class Signals:
    lane: str
    complexity: int
    needs_web: bool
    confident: bool
    why: list[str] = field(default_factory=list)


def heuristic(text: str, total_chars: int = 0) -> Signals:
    t = text.strip()
    n = len(t)
    why: list[str] = []

    if total_chars >= LONG_CONTEXT_CHARS:
        return Signals("long", 4, False, True, [f"{total_chars:,} chars of context"])
    if _SMALLTALK.match(t):
        return Signals("chat", 1, False, True, ["small talk"])

    code_strong = bool(_CODE_BLOCK.search(t) or _TRACE.search(t))
    s = {
        "chat": 0.5,
        "writing": 2.0 * _hits(_WRITING, t, 2),
        "creative": 2.5 * _hits(_CREATIVE, t, 2),
        "code": (3.0 if code_strong else 0) + 1.5 * _hits(_CODE_SYNTAX, t, 2) + 1.0 * _hits(_CODE_WORDS, t, 3),
        "reasoning": 2.0 * _hits(_MATH, t, 2) + 1.0 * _hits(_REASON, t, 3),
        # Error traces say "most recent call last": recency words don't count there.
        "research": (3.0 if _URL.search(t) else 0) + 3.0 * _hits(_SEARCH, t, 1)
                    + (0 if code_strong else 2.0 * _hits(_FRESH, t, 2)),
    }
    ranked = sorted(s.items(), key=lambda kv: kv[1], reverse=True)
    lane, top = ranked[0]
    margin = top - ranked[1][1]

    needs_web = s["research"] >= 2.0
    if needs_web and lane in ("chat", "writing"):
        lane = "research"
    if needs_web:
        why.append("needs current/web info" if not _URL.search(t) else "reads a URL")
    if code_strong:
        why.append("code or error trace")

    # Complexity: length, depth markers, domain.
    c = 2
    c += (n > 400) + (n > 2000) + (n > 8000)
    c += _hits(_REASON, t, 3) >= 2
    c += bool(_MATH.search(t)) and lane == "reasoning"
    c += bool(_PROOF.search(t))
    c += 2 * bool(_DEEP.search(t))
    c += 2 * bool(_SEARCH.search(t) and re.search(r"\b(deep|thorough|comprehensive)\b", t, _I))
    c -= bool(_QUICK.search(t))
    if lane in ("reasoning", "code") and top >= 3:
        c = max(c, 3)
    if lane == "chat" and n < 80:
        c = min(c, 2)
    c = max(1, min(5, c))

    confident = (
        n < 60
        or (top >= 3 and margin >= 1.5)
        or bool(_URL.search(t))
        or code_strong
        or bool(_DEEP.search(t))
    )
    why.insert(0, f"{lane} signals" if top > 0.5 else "general question")
    return Signals(lane, c, needs_web, confident, why)


# ============================================================
# LLM CLASSIFIER
# ============================================================

_CLASSIFY_PROMPT = """You are the routing classifier of an AI gateway. Do not answer the request and do not use any tools.
Classify the request below and reply with ONLY one JSON object, no prose:
{"lane": "...", "complexity": N, "needs_web": true|false}

lane — one of:
  chat      small talk, or a simple question answerable from general knowledge
  writing   emails, summaries, rewriting, proofreading, translation
  creative  stories, poems, lyrics, jokes, brainstorming, naming
  code      programming, debugging, software design, tooling
  reasoning math, science, logic, analysis, comparisons, planning, hard multi-step problems
  research  needs current events, live data, recent releases, or reading a specific web page
complexity — 1 trivial, 2 easy, 3 moderate, 4 hard (expert, multi-step), 5 very hard (research-grade, long rigorous work)
needs_web — true only if a good answer needs information from after 2025, live data, or a URL's contents

<request>
{request}
</request>"""

_JSON_OBJ = re.compile(r"\{[^{}]*\}", re.S)


class _LLMClassifier:
    SESSION = "__auto_router__"
    RESET_EVERY = 20

    def __init__(self) -> None:
        self.uses = 0
        self.cache: OrderedDict[str, Signals] = OrderedDict()
        self._lock = asyncio.Lock()

    def _target(self) -> Optional[tuple[Provider, str]]:
        keys = [_HAIKU, "antigravity:gemini-3.8-flash-low", "antigravity:gemini-3.7-flash-low"]
        if registry.api_preferred:
            keys.insert(0, "claude-api:claude-haiku-4-5")
        for key in keys:
            pid, model = key.split(":", 1)
            p = registry.get(pid)
            if p and p.available and any(m["id"] == model for m in p.models()):
                return p, model
        return None

    async def classify(self, text: str) -> Optional[Signals]:
        key = text[-4000:]
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        target = self._target()
        if not target:
            return None
        p, model = target

        if p.pool is None:        # API: stateless and parallel
            return await self._run(p, model, None, key)
        # CLI: one warm process serves every classification. When it is busy,
        # don't queue behind it: the caller falls back to the heuristic.
        if self._lock.locked():
            return None
        async with self._lock:
            self.uses += 1
            if self.uses % self.RESET_EVERY == 0:   # keep its own history small
                await p.pool.drop(self.SESSION)
            return await self._run(p, model, self.SESSION, key)

    async def _run(self, p: Provider, model: str, session: Optional[str], key: str) -> Optional[Signals]:
        prompt = _CLASSIFY_PROMPT.replace("{request}", key)
        chunks: list[str] = []

        async def run() -> None:
            async for ev in p.chat([Msg("user", prompt)], model, session, "low"):
                if ev["type"] == "text":
                    chunks.append(ev["content"])
                elif ev["type"] == "result" and not chunks:
                    chunks.append(ev.get("content") or "")

        try:
            await asyncio.wait_for(run(), timeout=config.AUTO_CLASSIFIER_TIMEOUT)
        except Exception:
            return None

        m = _JSON_OBJ.search("".join(chunks))
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            lane = str(d.get("lane", "")).lower()
            c = int(d.get("complexity", 0))
        except (ValueError, TypeError):
            return None
        if lane not in LANES or not 1 <= c <= 5:
            return None
        sig = Signals(lane, c, bool(d.get("needs_web")), True, [f"classified by {model}"])
        self.cache[key] = sig
        while len(self.cache) > 256:
            self.cache.popitem(last=False)
        return sig


# ============================================================
# ROUTER
# ============================================================

@dataclass
class Route:
    lane: str
    complexity: int
    needs_web: bool
    method: str                                   # heuristic | llm | sticky | orchestrator
    reason: str
    candidates: list[tuple[Provider, str, str]]   # (provider, model, effort)
    escalate: bool = False                        # worth the full orchestration pipeline

    def describe(self, i: int = 0) -> dict:
        p, model, effort = self.candidates[i]
        return {
            "type": "route", "provider": p.id, "model": model, "effort": effort or None,
            "lane": self.lane, "complexity": self.complexity, "needs_web": self.needs_web,
            "method": self.method, "reason": self.reason, "attempt": i + 1,
            "fallbacks": [f"{q.id}:{m}" for q, m, _ in self.candidates[i + 1:]],
        }


_LIST_ITEM = re.compile(r"^\s*(\d+[.)]|[-*•])\s+\S", _M)


def _multi_part(text: str) -> bool:
    """Several distinct asks: 3+ list items or 3+ questions."""
    return len(_LIST_ITEM.findall(text)) >= 3 or text.count("?") >= 3


def _last_user(messages: list[Msg]) -> tuple[str, str]:
    users = [m.content for m in messages if m.role == "user" and m.content.strip()]
    if not users:
        return "", ""
    return users[-1], (users[-2] if len(users) > 1 else "")


class AutoRouter:
    def __init__(self) -> None:
        self.llm = _LLMClassifier()
        self._last: OrderedDict[str, tuple[str, int]] = OrderedDict()   # session → (lane, complexity)

    def info(self) -> dict:
        return {"model": AUTO_ID, "classifier": config.AUTO_CLASSIFIER,
                "max_attempts": config.AUTO_MAX_ATTEMPTS, "lanes": list(LANES)}

    async def route(
        self,
        messages: list[Msg],
        session_id: Optional[str] = None,
        agent: bool = False,
    ) -> Route:
        text, prev = _last_user(messages)
        total = sum(len(m.content) for m in messages)

        # A short follow-up only makes sense with the question before it.
        follow_up = bool(prev) and len(text) < 120 and not _SMALLTALK.match(text.strip())
        subject = f"{prev[-1500:]}\n\n{text}" if follow_up else text

        sig = heuristic(subject, total)
        method = "heuristic"
        mode = config.AUTO_CLASSIFIER
        if mode == "llm" or (mode == "hybrid" and not sig.confident):
            llm = await self.llm.classify(subject)
            if llm:
                # Keep hard facts the patterns are sure about.
                llm.needs_web = llm.needs_web or bool(_URL.search(text))
                sig, method = llm, "llm"

        lane, c = sig.lane, sig.complexity
        if agent and lane in ("chat", "writing", "research"):
            lane, c = "code", max(c, 3)
        if sig.needs_web and lane in ("chat", "writing"):
            lane = "research"

        # Within a conversation, never step down in the same lane.
        if session_id:
            prev_route = self._last.get(session_id)
            if prev_route and prev_route[0] == lane and prev_route[1] > c:
                c, method = prev_route[1], "sticky"
            self._last[session_id] = (lane, c)
            self._last.move_to_end(session_id)
            while len(self._last) > 1000:
                self._last.popitem(last=False)

        candidates = _available(_plan(lane, c))
        reason = "; ".join(sig.why + (["follow-up"] if follow_up else []))
        # Orchestration multiplies calls and latency: only for requests that
        # are very hard, or clearly made of several separate asks.
        escalate = config.AUTO_ESCALATE and not agent and not follow_up and (
            (c >= 5 and lane in ("reasoning", "code", "research"))
            or (c >= 3 and _multi_part(text))
        )
        return Route(lane, c, sig.needs_web, method, reason, candidates, escalate)


router = AutoRouter()

WEB_HINT = (
    "\n\n[Routing note: this request needs up-to-date or external information. "
    "Use WebSearch / WebFetch to check it, and cite the sources you used.]"
)


def with_web_hint(messages: list[Msg]) -> list[Msg]:
    """Nudge the model to actually use its web tools on research turns."""
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].role == "user":
            out[i] = Msg("user", out[i].content + WEB_HINT)
            break
    return out
