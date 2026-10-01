"""
switchboard_ai.installer
────────────────────
`switchboard-ai setup`: find the provider CLIs and install missing ones with
their official installers.

    claude  ← https://claude.ai/install.sh               (Windows: .ps1)
    agy     ← https://antigravity.google/cli/install.sh  (Windows: .ps1)

Both installers put the binary in ~/.local/bin, which discovery.py searches,
so a fresh install is picked up without touching PATH or .env. Nothing is
installed without asking unless --yes is given.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from switchboard_ai import config
from switchboard_ai.discovery import find_agy_binaries, find_claude_binaries, resolve


@dataclass(frozen=True)
class Tool:
    provider: str                  # registry id
    label: str
    script: str                    # installer URL without .sh / .ps1
    finder: Callable
    config_path: str               # name of the *_BINARY_PATH setting
    enabled: bool
    sign_in_args: tuple[str, ...]  # first run that opens the sign-in flow
    sign_in_hint: str


TOOLS: dict[str, Tool] = {
    "claude": Tool(
        "claude", "Claude Code", "https://claude.ai/install",
        find_claude_binaries, "CLAUDE_BINARY_PATH", config.ENABLE_CLAUDE,
        (), "Sign in in the window that opens, then type /exit.",
    ),
    "antigravity": Tool(
        "antigravity", "Antigravity CLI (agy)", "https://antigravity.google/cli/install",
        find_agy_binaries, "AGY_BINARY_PATH", config.ENABLE_ANTIGRAVITY,
        ("-p", "say hi"), "A browser window opens to sign in with your Google account.",
    ),
}


def find(tool: Tool) -> Optional[str]:
    return resolve(getattr(config, tool.config_path), tool.finder)


def version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "version unknown"


def install_command(tool: Tool) -> Optional[list[str]]:
    """The official one-line installer for this OS, or None if it can't run here."""
    if os.name == "nt":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        return [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                f"irm {tool.script}.ps1 | iex"] if ps else None
    if shutil.which("curl") and shutil.which("bash"):
        return ["bash", "-c", f"curl -fsSL {tool.script}.sh | bash"]
    return None


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"  {question} [skipped: not an interactive terminal, use --yes]")
        return False
    try:
        return input(f"  {question} [Y/n] ").strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


def _install(tool: Tool, assume_yes: bool) -> Optional[str]:
    """Run the installer; returns the binary found afterwards."""
    cmd = install_command(tool)
    if not cmd:
        need = "PowerShell" if os.name == "nt" else "curl and bash"
        print(f"  Can't install automatically: {need} not found.")
        return None
    if not _confirm(f"Install {tool.label} now? (runs: {cmd[-1]})", assume_yes):
        return None
    print()
    if subprocess.run(cmd).returncode != 0:
        print(f"\n  {tool.label} installer failed.")
        return None
    binary = find(tool)
    if not binary:
        print(f"\n  Installed, but the binary wasn't found. Set {tool.config_path} in .env.")
    return binary


def _sign_in(tool: Tool, binary: str, assume_yes: bool) -> None:
    if assume_yes or not sys.stdin.isatty():
        print(f"  Sign in later by running: {binary} {' '.join(tool.sign_in_args)}".rstrip())
        return
    if _confirm(f"Sign in to {tool.label} now?", False):
        print(f"  {tool.sign_in_hint}\n")
        subprocess.run([binary, *tool.sign_in_args])


def run_setup(only: Optional[list[str]] = None, assume_yes: bool = False,
              update: bool = False, check: bool = False) -> int:
    """Report each provider CLI; install (or with update, reinstall) as asked.
    Returns 1 when an enabled provider is still missing."""
    missing = 0
    for tool in TOOLS.values():
        if only and tool.provider not in only:
            continue
        print(f"\n[{tool.label}]")
        if not tool.enabled:
            print("  disabled in .env, skipped")
            continue

        binary = find(tool)
        if binary:
            print(f"  found    : {binary}")
            print(f"  version  : {version(binary)}")
            if not update or check:
                continue
        else:
            print("  not found")
            if check:
                missing += 1
                continue

        fresh = _install(tool, assume_yes)
        if fresh:
            print(f"\n  installed: {fresh}")
            print(f"  version  : {version(fresh)}")
            if not binary:
                _sign_in(tool, fresh, assume_yes)
        elif not binary:
            missing += 1

    print()
    if missing:
        print("Some providers are missing. Run `switchboard-ai setup` again to install them.")
    else:
        print("Ready. Start the server with: python -m switchboard_ai server")
    return 1 if missing else 0
