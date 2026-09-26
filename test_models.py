"""
Switchboard AI — model test runner
──────────────────────────────
Sends test prompts to every model on a running Switchboard AI server and
reports, per request and per model:

    status · pass/fail check · time to first token · total time
    input / output / cache / thinking tokens · cost · which model answered

Run (server must already be running on port 8000):

    python switchboard_ai/test_models.py --full --concurrency 4   # EVERYTHING in one run
    python switchboard_ai/test_models.py                  # each model gets a different prompt
    python switchboard_ai/test_models.py --mode all       # every model runs every test case
    python switchboard_ai/test_models.py --models gemini-3.8-flash-low claude-sonnet-5
    python switchboard_ai/test_models.py --virtual        # also auto-orchestrate / auto-ensemble
    python switchboard_ai/test_models.py --concurrency 4  # run 4 requests at once
    python switchboard_ai/test_models.py --list           # show the test cases and exit

A JSON report with every answer is written next to this file
(test_report_<timestamp>.json) unless --no-report is given.

Cost:
  • Claude models: the cost reported by the server (claude.exe / Claude API),
    or, if that is 0, an estimate from Anthropic API list prices below.
    On a Pro/Max subscription this is the API-equivalent value, not a bill.
  • Antigravity models (Gemini, GPT-OSS, …): the CLI reports no price, so
    cost shows "n/a" (usage counts against your Google plan quota instead).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    import httpx
except ImportError:
    sys.exit("This script needs httpx:  pip install httpx")

DEFAULT_BASE = "http://127.0.0.1:8000"

# Anthropic API list prices, USD per 1M tokens: (input, output).
# Cache reads bill 0.1x input, cache writes 1.25x input.
CLAUDE_PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5-20251101": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5-20250929": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


# ============================================================
# TEST CASES
# ============================================================

def _has(*words: str) -> Callable[[str], bool]:
    return lambda text: all(w.lower() in text.lower() for w in words)


def _any(*words: str) -> Callable[[str], bool]:
    return lambda text: any(w.lower() in text.lower() for w in words)


def _json_paris(text: str) -> bool:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return False
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False
    return "paris" in str(d.get("city", "")).lower() and "france" in str(d.get("country", "")).lower()


def _haiku(text: str) -> bool:
    lines = [l for l in text.strip().splitlines() if l.strip() and not l.strip().startswith("#")]
    return 3 <= len(lines) <= 6


@dataclass
class Case:
    id: str
    category: str
    prompt: str
    check: Callable[[str], bool]
    expect: str          # human-readable description of the check


CASES: list[Case] = [
    Case("greeting", "chat",
         "Hi! In one sentence: which AI model are you, and who made you?",
         lambda t: len(t.strip()) > 10, "any non-trivial reply"),
    Case("capital", "factual",
         "What is the capital city of Australia? Answer with only the city name.",
         _has("canberra"), "contains 'Canberra'"),
    Case("arithmetic", "math",
         "What is 17 multiplied by 23? Reply with only the number.",
         _has("391"), "contains '391'"),
    Case("bat_ball", "reasoning",
         "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
         "How much does the ball cost? Give the answer in one short sentence.",
         _any("0.05", "5 cents", "five cents"), "says $0.05 / 5 cents"),
    Case("is_prime", "code",
         "Write a Python function is_prime(n) that returns True if n is a prime number. "
         "Return only the code in one Python code block.",
         _has("def is_prime"), "defines is_prime"),
    Case("json_format", "structured output",
         'In which city and country is the Eiffel Tower? Reply with ONLY this JSON, no prose: '
         '{"city": "...", "country": "..."}',
         _json_paris, "valid JSON with Paris / France"),
    Case("sql", "nl-to-sql",
         "Tables: customers(id, name) and orders(id, customer_id, amount). Write one SQL query "
         "that returns the top 5 customers by total order amount. Return only the SQL.",
         _has("select", "sum(", "group by"), "SELECT … SUM( … GROUP BY"),
    Case("translate", "language",
         "Translate into French: 'Good morning, how are you?' Reply with only the translation.",
         _any("bonjour"), "contains 'Bonjour'"),
    Case("summarize", "summarization",
         "Summarize in one sentence: The Great Barrier Reef, off the coast of Queensland, "
         "Australia, is the world's largest coral reef system. It is made of over 2,900 "
         "individual reefs and 900 islands spread over 2,300 kilometres, can be seen from "
         "outer space, and is threatened by climate change, pollution and coral bleaching.",
         _any("reef"), "mentions the reef"),
    Case("haiku", "creative",
         "Write a haiku about monsoon rain. Only the poem.",
         _haiku, "3-6 non-empty lines"),
]


# ============================================================
# RESULT
# ============================================================

@dataclass
class Result:
    model_key: str
    model_name: str
    provider: str
    case_id: str
    category: str
    status: str = "?"                  # OK | FAIL (HTTP/stream error) | EMPTY
    http: Optional[int] = None
    passed: Optional[bool] = None
    ttft_s: Optional[float] = None
    total_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_source: str = "n/a"          # reported | estimated | n/a
    answered_by: str = ""
    route: str = ""
    tools: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    error: str = ""
    answer: str = ""

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


def _estimate_cost(model_id: str, r: Result) -> Optional[float]:
    price = CLAUDE_PRICES.get(model_id)
    if not price:
        return None
    pin, pout = price
    billed_in = r.input_tokens + 0.1 * r.cache_read_tokens + 1.25 * r.cache_write_tokens
    return (billed_in * pin + r.output_tokens * pout) / 1_000_000


# ============================================================
# ONE REQUEST  (/chat/stream, so time-to-first-token is measurable)
# ============================================================

async def run_one(client: httpx.AsyncClient, base: str, headers: dict,
                  model: dict, case: Case, timeout: float) -> Result:
    r = Result(model_key=model["key"], model_name=model["name"], provider=model["provider"],
               case_id=case.id, category=case.category)
    body = {"model": model["key"], "messages": [{"role": "user", "content": case.prompt}]}
    text: list[str] = []
    final: dict = {}
    t0 = time.perf_counter()
    try:
        async with client.stream("POST", f"{base}/chat/stream", json=body, headers=headers,
                                 timeout=timeout) as resp:
            r.http = resp.status_code
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", errors="replace")
                try:
                    r.error = json.loads(raw).get("detail", raw)
                except json.JSONDecodeError:
                    r.error = raw
                r.status = "FAIL"
                return r
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "text":
                    if r.ttft_s is None:
                        r.ttft_s = time.perf_counter() - t0
                    text.append(ev.get("content", ""))
                elif t == "route":
                    r.answered_by = f"{ev.get('provider')}:{ev.get('model')}"
                    r.route = f"{ev.get('lane')} · effort {ev.get('effort') or '-'} · {ev.get('method')}"
                elif t == "tool_use" and ev.get("detail") is not None:
                    name = ev.get("name", "tool")
                    if name not in r.tools:
                        r.tools.append(name)
                elif t == "notice":
                    r.notices.append(ev.get("content", ""))
                elif t == "error":
                    r.error = ev.get("content", "error")
                elif t == "result":
                    final = ev
    except httpx.TimeoutException:
        r.error = f"timed out after {timeout:.0f}s"
    except httpx.HTTPError as e:
        r.error = f"{type(e).__name__}: {e}"
    r.total_s = time.perf_counter() - t0

    r.answer = "".join(text).strip()
    u = final.get("usage") or {}
    r.input_tokens = u.get("input_tokens") or 0
    r.output_tokens = u.get("output_tokens") or 0
    r.cache_read_tokens = u.get("cache_read_input_tokens") or 0
    r.cache_write_tokens = u.get("cache_creation_input_tokens") or 0
    r.thinking_tokens = u.get("thinking_tokens") or 0
    if not r.answered_by:
        r.answered_by = f"{model['provider']}:{model['id']}"

    answered_pid, _, answered_id = r.answered_by.partition(":")
    reported = final.get("cost_usd") or 0
    if reported:
        r.cost_usd, r.cost_source = float(reported), "reported"
    elif answered_pid in ("claude", "claude-api"):
        # Only Anthropic-billed providers get an API-price estimate; Claude
        # models served by Antigravity run on the Google plan.
        est = _estimate_cost(answered_id, r)
        if est is not None:
            r.cost_usd, r.cost_source = est, "estimated"

    if r.answer:
        r.status = "OK"
        r.passed = bool(case.check(r.answer))
    else:
        r.status = "FAIL" if r.error else "EMPTY"
        r.passed = False
    return r


# ============================================================
# TERMINAL OUTPUT
# ============================================================

class C:
    """ANSI colours (disabled with --no-color or when not a terminal)."""
    on = True
    G, R, Y, B, D, BOLD, END = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[1m", "\033[0m"

    @classmethod
    def c(cls, code: str, s: str) -> str:
        return f"{code}{s}{cls.END}" if cls.on else s


def _num(n: int) -> str:
    return f"{n:,}" if n else "-"


def _secs(x: Optional[float]) -> str:
    return f"{x:.2f}" if x is not None else "-"


def _cost(x: Optional[float]) -> str:
    return f"${x:.5f}" if x is not None else "n/a"


def _cut(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def print_row(i: int, total: int, r: Result) -> None:
    if r.status == "OK":
        state = C.c(C.G, "PASS") if r.passed else C.c(C.Y, "CHECK-FAIL")
    else:
        state = C.c(C.R, r.status)
    head = f"[{i:>2}/{total}] {state:<10} {C.c(C.BOLD, r.model_key)}  ·  {r.case_id} ({r.category})"
    print(head)
    print(f"        time: first token {_secs(r.ttft_s)} s · total {_secs(r.total_s)} s"
          f"   tokens: in {_num(r.total_input)} (cache read {_num(r.cache_read_tokens)}, "
          f"write {_num(r.cache_write_tokens)}) · out {_num(r.output_tokens)}"
          + (f" · thinking {_num(r.thinking_tokens)}" if r.thinking_tokens else "")
          + f"   cost: {_cost(r.cost_usd)}" + (f" ({r.cost_source})" if r.cost_usd is not None else ""))
    if r.model_key.startswith("auto") or r.route:
        print(C.c(C.B, f"        routed to {r.answered_by}  [{r.route}]"))
    if r.tools:
        print(C.c(C.D, f"        tools: {', '.join(r.tools)}"))
    for n in r.notices:
        print(C.c(C.Y, f"        notice: {_cut(n, 150)}"))
    if r.error:
        print(C.c(C.R, f"        error: {_cut(r.error, 200)}"))
    if r.answer:
        print(C.c(C.D, f"        answer: {_cut(r.answer, 150)}"))
    print()


def print_summary(results: list[Result], wall: float) -> None:
    by_model: dict[str, list[Result]] = {}
    for r in results:
        by_model.setdefault(r.model_key, []).append(r)

    cols = ("MODEL", "RUNS", "OK", "PASS", "AVG 1ST TOK", "AVG TOTAL", "IN TOKENS", "OUT TOKENS", "COST")
    widths = (44, 5, 4, 5, 12, 10, 12, 11, 12)
    line = "  ".join(f"{h:<{w}}" for h, w in zip(cols, widths))
    print(C.c(C.BOLD, "=" * len(line)))
    print(C.c(C.BOLD, "SUMMARY PER MODEL"))
    print(C.c(C.BOLD, "=" * len(line)))
    print(C.c(C.BOLD, line))
    print("-" * len(line))

    for key, rs in by_model.items():
        ok = [r for r in rs if r.status == "OK"]
        ttfts = [r.ttft_s for r in ok if r.ttft_s is not None]
        totals = [r.total_s for r in ok]
        costs = [r.cost_usd for r in rs if r.cost_usd is not None]
        cells = (
            key, str(len(rs)), str(len(ok)), str(sum(1 for r in rs if r.passed)),
            f"{sum(ttfts) / len(ttfts):.2f} s" if ttfts else "-",
            f"{sum(totals) / len(totals):.2f} s" if totals else "-",
            _num(sum(r.total_input for r in rs)), _num(sum(r.output_tokens for r in rs)),
            _cost(sum(costs)) if costs else "n/a",
        )
        row = "  ".join(f"{_cut(c, w):<{w}}" for c, w in zip(cells, widths))
        colour = C.G if ok and len(ok) == len(rs) else (C.Y if ok else C.R)
        print(C.c(colour, row))

    ok = [r for r in results if r.status == "OK"]
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    fastest = min(ok, key=lambda r: r.total_s, default=None)
    slowest = max(ok, key=lambda r: r.total_s, default=None)
    print("-" * len(line))
    print(C.c(C.BOLD, "TOTALS"))
    print(f"  requests          : {len(results)}  ·  answered {len(ok)}  ·  "
          f"checks passed {sum(1 for r in results if r.passed)}  ·  failed {len(results) - len(ok)}")
    print(f"  models tested     : {len(by_model)}  ·  "
          f"fully working {sum(1 for rs in by_model.values() if all(r.status == 'OK' for r in rs))}")
    print(f"  input tokens      : {sum(r.total_input for r in results):,}  "
          f"(cache read {sum(r.cache_read_tokens for r in results):,}, "
          f"cache write {sum(r.cache_write_tokens for r in results):,})")
    print(f"  output tokens     : {sum(r.output_tokens for r in results):,}"
          + (f"  ·  thinking {sum(r.thinking_tokens for r in results):,}"
             if any(r.thinking_tokens for r in results) else ""))
    print(f"  total cost        : {_cost(sum(costs)) if costs else 'n/a'}  "
          f"(reported {sum(1 for r in results if r.cost_source == 'reported')}, "
          f"estimated {sum(1 for r in results if r.cost_source == 'estimated')}, "
          f"no price {sum(1 for r in results if r.cost_usd is None)})")
    if ok:
        print(f"  avg total time    : {sum(r.total_s for r in ok) / len(ok):.2f} s per answered request")
    if fastest:
        print(f"  fastest           : {fastest.model_key} ({fastest.total_s:.2f} s)")
    if slowest:
        print(f"  slowest           : {slowest.model_key} ({slowest.total_s:.2f} s)")
    print(f"  wall-clock time   : {wall:.1f} s")

    failed = [r for r in results if r.status != "OK"]
    if failed:
        print(C.c(C.R, "\nFAILED REQUESTS"))
        for r in failed:
            print(C.c(C.R, f"  {r.model_key} · {r.case_id}: HTTP {r.http} · {_cut(r.error or r.status, 160)}"))
    wrong = [r for r in results if r.status == "OK" and not r.passed]
    if wrong:
        print(C.c(C.Y, "\nANSWERED BUT CHECK FAILED"))
        for r in wrong:
            case = next(c for c in CASES if c.id == r.case_id)
            print(C.c(C.Y, f"  {r.model_key} · {r.case_id} (expected {case.expect}): {_cut(r.answer, 110)}"))


# ============================================================
# MAIN
# ============================================================

async def main() -> int:
    ap = argparse.ArgumentParser(description="Test every model on a running Switchboard AI server.")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"server URL (default {DEFAULT_BASE})")
    ap.add_argument("--api-key", default=os.getenv("SWITCHBOARD_API_KEY", ""),
                    help="API key if the server has API_KEY set (or env SWITCHBOARD_API_KEY)")
    ap.add_argument("--mode", choices=("rotate", "all"), default="rotate",
                    help="rotate: each model gets a different test case (default); all: every case on every model")
    ap.add_argument("--cases", nargs="*", help="only these test case ids (see --list)")
    ap.add_argument("--models", nargs="*", help="only models whose key/id contains one of these strings")
    ap.add_argument("--virtual", action="store_true",
                    help="also test auto-orchestrate and auto-ensemble (slow: several model calls each)")
    ap.add_argument("--no-auto", action="store_true", help="skip the 'auto' model")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="requests at once (default 1 = sequential, cleanest timings)")
    ap.add_argument("--timeout", type=float, default=600, help="seconds per request (default 600)")
    ap.add_argument("--no-report", action="store_true", help="don't write the JSON report")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--list", action="store_true", help="list the test cases and exit")
    ap.add_argument("--full", action="store_true",
                    help="everything in one run: every test case on every model, including "
                         "auto, auto-orchestrate and auto-ensemble")
    args = ap.parse_args()
    if args.full:
        args.mode, args.virtual, args.no_auto = "all", True, False

    if args.no_color or not sys.stdout.isatty():
        C.on = False
    elif os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console

    if args.list:
        for c in CASES:
            print(f"{c.id:<12} {c.category:<18} expects {c.expect}\n             {c.prompt}\n")
        return 0

    cases = [c for c in CASES if not args.cases or c.id in args.cases]
    if not cases:
        print("No test cases match --cases. Use --list to see the ids.")
        return 2
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}

    async with httpx.AsyncClient() as client:
        try:
            health = (await client.get(f"{args.base}/health", headers=headers, timeout=15)).json()
            models = (await client.get(f"{args.base}/models", headers=headers, timeout=15)).json()
        except Exception as e:
            print(f"Cannot reach the server at {args.base}: {e}\nStart it with: python -m switchboard_ai server")
            return 2

        # Which models to test.
        selected = []
        for m in models:
            if not m.get("available"):
                continue
            if m["provider"] == "auto":
                if m["key"] == "auto" and args.no_auto:
                    continue
                if m["key"] != "auto" and not args.virtual:
                    continue
            if args.models and not any(s.lower() in m["key"].lower() for s in args.models):
                continue
            selected.append(m)
        if not selected:
            print("No available models match. Check GET /models.")
            return 2

        # Build the job list.
        if args.mode == "all":
            jobs = [(m, c) for m in selected for c in cases]
        else:
            jobs = [(m, cases[i % len(cases)]) for i, m in enumerate(selected)]

        prov = ", ".join(f"{p['label']}: {'up' if p.get('available') else 'down'}"
                         for p in health.get("providers", {}).values())
        print(C.c(C.BOLD, "\nSWITCHBOARD AI — MODEL TEST"))
        print(f"  server      : {args.base}  (v{health.get('version', '?')})")
        print(f"  providers   : {prov}")
        print(f"  models      : {len(selected)} of {len(models)} listed"
              f"  ·  test cases: {len(cases)}  ·  mode: {args.mode}")
        print(f"  requests    : {len(jobs)}  ·  concurrency: {args.concurrency}  ·  timeout: {args.timeout:.0f}s")
        print(f"  started     : {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        print(C.c(C.BOLD, "TEST CASES"))
        for c in cases:
            print(f"  {c.id:<12} {c.category:<18} expects {c.expect}")
        print()

        sem = asyncio.Semaphore(max(1, args.concurrency))
        results: list[Result] = []
        done = 0
        t_start = time.perf_counter()

        async def worker(m: dict, c: Case) -> None:
            nonlocal done
            async with sem:
                r = await run_one(client, args.base, headers, m, c, args.timeout)
            results.append(r)
            done += 1
            print_row(done, len(jobs), r)

        try:
            await asyncio.gather(*(worker(m, c) for m, c in jobs))
        except KeyboardInterrupt:
            print("\nInterrupted — summarising what finished.\n")
        wall = time.perf_counter() - t_start

    order = {(m["key"], c.id): i for i, (m, c) in enumerate(jobs)}
    results.sort(key=lambda r: order.get((r.model_key, r.case_id), 0))
    print_summary(results, wall)

    if not args.no_report:
        path = Path(__file__).parent / f"test_report_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps({
            "server": args.base,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "mode": args.mode,
            "wall_seconds": round(wall, 2),
            "results": [{**asdict(r), "total_input_tokens": r.total_input,
                         "ttft_s": round(r.ttft_s, 3) if r.ttft_s is not None else None,
                         "total_s": round(r.total_s, 3)} for r in results],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nFull report (every answer): {path}")

    return 0 if all(r.status == "OK" for r in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
