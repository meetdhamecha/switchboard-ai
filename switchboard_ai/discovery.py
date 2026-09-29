"""
switchboard_ai.discovery
────────────────────
Find the provider binaries on this machine.

    claude.exe  ← Claude Code extension (Antigravity IDE / VS Code / Cursor /
                  Windsurf / VS Code Insiders) or a standalone Claude Code install
    agy.exe     ← Antigravity CLI (~/.gemini/bin/agy.exe)

Either, both, or neither may be present; the server adapts to whatever is found.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

HOME = Path.home()

_CLAUDE_EXT_DIRS = [
    HOME / ".antigravity-ide" / "extensions",
    HOME / ".antigravity" / "extensions",
    HOME / ".vscode" / "extensions",
    HOME / ".vscode-insiders" / "extensions",
    HOME / ".cursor" / "extensions",
    HOME / ".windsurf" / "extensions",
]

_EXE = ".exe" if os.name == "nt" else ""


def _version_key(path: Path) -> tuple:
    """Sort key: the extension's version number, e.g. 2.1.280 → (2, 1, 280)."""
    m = re.search(r"claude-code-(\d+(?:\.\d+)*)", str(path))
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def find_claude_binaries() -> list[Path]:
    """Every claude binary found, newest extension version first."""
    found: list[Path] = []
    for ext_dir in _CLAUDE_EXT_DIRS:
        if ext_dir.is_dir():
            found.extend(
                ext_dir.glob(f"anthropic.claude-code-*/resources/native-binary/claude{_EXE}")
            )
    found.sort(key=_version_key, reverse=True)

    # Standalone installs come after IDE copies.
    for p in (HOME / ".local" / "bin" / f"claude{_EXE}",):
        if p.is_file():
            found.append(p)
    on_path = shutil.which("claude")
    if on_path and on_path.lower().endswith(_EXE or "claude"):
        found.append(Path(on_path))

    seen, unique = set(), []
    for p in found:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def find_agy_binaries() -> list[Path]:
    """Every agy binary found, preferred location first."""
    candidates = [HOME / ".gemini" / "bin" / f"agy{_EXE}"]
    on_path = shutil.which("agy")
    if on_path:
        candidates.append(Path(on_path))
    local = os.getenv("LOCALAPPDATA")
    if local:
        for app in ("Antigravity", "Antigravity IDE"):
            root = Path(local) / "Programs" / app
            if root.is_dir():
                candidates.extend(root.glob(f"**/bin/agy{_EXE}"))
    return [p for p in candidates if p.is_file()]


def resolve(configured: str, finder) -> Optional[str]:
    """
    Explicit path from .env wins if it exists; "auto"/empty searches.
    Returns None when nothing usable is found.
    """
    if configured and configured.lower() != "auto":
        return configured if os.path.isfile(configured) else None
    hits = finder()
    return str(hits[0]) if hits else None


def claude_logged_in() -> bool:
    """claude.exe stores its OAuth tokens here once the user has signed in."""
    from switchboard_ai.auth import ClaudeAuth
    return ClaudeAuth.is_present() and ClaudeAuth.get_credentials().get("logged_in", False)


def antigravity_logged_in() -> bool:
    """Antigravity stores its account credentials in state.vscdb."""
    from switchboard_ai.auth import AntigravityAuth
    return AntigravityAuth.get_credentials().get("logged_in", False)

