"""
switchboard_ai.main
───────────────
    python -m switchboard_ai server [--host H] [--port P]
    python -m switchboard_ai status
    python -m switchboard_ai ask "question" [--model M]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

# Allow `python switchboard_ai/main.py …` as well as `python -m switchboard_ai …`.
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def _status() -> None:
    from switchboard_ai import config
    from switchboard_ai.providers import registry

    for p in registry.providers.values():
        info = p.info()
        mark = "OK " if p.available else "-- "
        print(f"[{mark}] {p.label}")
        print(f"      binary   : {p.binary or 'not found'}")
        if "logged_in" in info:
            print(f"      signed in: {'yes' if info['logged_in'] else 'no'}")
        acc = info.get("account")
        if acc:
            if "email" in acc and acc["email"]:
                print(f"      account  : {acc['email']} ({acc.get('name', '')})")
            if "subscription" in acc and acc["subscription"]:
                print(f"      plan     : {acc['subscription']}")
            if "access_token" in acc and acc["access_token"]:
                print(f"      token    : {acc['access_token']}")
        if p.available:
            print(f"      models   : {', '.join(m['id'] for m in p.models())}")
        print()
    dp, dm = registry.default()
    print(f"Default model: {f'{dp.id}:{dm}' if dp else 'none — no provider available'}")
    print(f"Agent shell  : {'ENABLED' if config.AGENT_ALLOW_SHELL else 'off'}")


def _auth(reveal: bool = False, refresh: bool = False) -> None:
    from switchboard_ai import auth
    from switchboard_ai.providers import registry

    if refresh:
        print("Refreshing accounts and tokens...\n")
        cp = registry.get("claude")
        ap = registry.get("antigravity")
        auth.ClaudeAuth.refresh(cp.binary if cp else None)
        auth.AntigravityAuth.refresh(ap.binary if ap else None)

    st = auth.get_all_auth_status(reveal=reveal)
    
    print("=" * 64)
    print("  SWITCHBOARD AI — ACCOUNTS & TOKENS")
    print("=" * 64)

    # Claude
    c = st.get("claude", {})
    c_mark = "SIGNED IN" if c.get("logged_in") else "NOT SIGNED IN"
    print(f"\n[Claude Code] — {c_mark}")
    if c.get("available"):
        print(f"  Plan            : Claude {c.get('subscription', 'pro').capitalize()}")
        print(f"  Rate Tier       : {c.get('rate_limit_tier')}")
        print(f"  Org UUID        : {c.get('organization_uuid')}")
        print(f"  Access Token    : {c.get('access_token')}")
        print(f"    Expires in    : {c.get('access_token_expires_in')} ({c.get('access_token_expires_at')})")
        print(f"  Refresh Token   : {c.get('refresh_token')}")
        print(f"    Expires in    : {c.get('refresh_token_expires_in')}")
        print(f"  Credentials File: {c.get('credentials_file')}")
    else:
        print(f"  Status          : {c.get('error', 'Not available')}")

    # Antigravity
    a = st.get("antigravity", {})
    a_mark = "SIGNED IN" if a.get("logged_in") else "NOT SIGNED IN"
    print(f"\n[Antigravity] — {a_mark}")
    if a.get("available"):
        print(f"  User Name       : {a.get('name')}")
        print(f"  Email           : {a.get('email')}")
        print(f"  Subscription    : {a.get('subscription')}")
        print(f"  Access Token    : {a.get('access_token')}")
        print(f"  Refresh Token   : {a.get('refresh_token')}")
        print(f"  API Key         : {a.get('api_key')}")
        print(f"  State Database  : {a.get('database_file')}")
    else:
        print(f"  Status          : {a.get('error', 'Not available')}")

    print("\n" + "=" * 64)
    if not reveal:
        print("Tokens are masked. Use --reveal to display full unmasked tokens.")
    print()



async def _ask(prompt: str, model: str | None) -> int:
    from switchboard_ai.process import Msg
    from switchboard_ai.providers import RoutingError, registry

    try:
        p, m = registry.resolve(model, None)
    except RoutingError as e:
        print(f"error: {e.detail}", file=sys.stderr)
        return 1
    print(f"[{p.id}:{m}]\n", file=sys.stderr)
    code = 0
    async for ev in p.chat([Msg("user", prompt)], m, None, None):
        if ev["type"] == "text":
            print(ev["content"], end="", flush=True)
        elif ev["type"] == "tool_use" and not ev.get("detail"):
            print(f"\n  ⚙ {ev['name']}", file=sys.stderr)
        elif ev["type"] == "error":
            print(f"\nerror: {ev['content']}", file=sys.stderr)
            code = 1
    print()
    await p.shutdown()
    return code


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    parser = argparse.ArgumentParser(prog="switchboard-ai", description="Switchboard AI — smart router for AI models")
    sub = parser.add_subparsers(dest="command")

    s = sub.add_parser("server", help="Start the API server and web UI")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)

    sub.add_parser("status", help="Show which providers were found")

    auth_p = sub.add_parser("auth", help="Show account auth and token details")
    auth_p.add_argument("--reveal", action="store_true", help="Show full unmasked tokens")
    auth_p.add_argument("--refresh", action="store_true", help="Force refresh tokens")

    a = sub.add_parser("ask", help="One-off question from the terminal")
    a.add_argument("prompt")
    a.add_argument("--model", default=None)

    args = parser.parse_args()

    if args.command == "server":
        import uvicorn
        from switchboard_ai import config

        # Overrides must land in config so the startup banner shows them.
        config.API_HOST = args.host or config.API_HOST
        config.API_PORT = args.port or config.API_PORT
        # One process on purpose: warm sessions live in memory. Concurrency comes
        # from asyncio; backlog lets bursts of connections queue at the socket.
        uvicorn.run("switchboard_ai.server:app", host=config.API_HOST, port=config.API_PORT,
                    log_level="warning", backlog=2048, timeout_keep_alive=30)
    elif args.command == "status":
        _status()
    elif args.command == "auth":
        _auth(reveal=args.reveal, refresh=args.refresh)
    elif args.command == "ask":
        sys.exit(asyncio.run(_ask(args.prompt, args.model)))
    else:
        parser.print_help()



if __name__ == "__main__":
    main()
