"""
switchboard_ai.discovery
────────────────────
Find the provider binaries on this machine (Windows, macOS, Linux).

    claude  ← Claude Code extension (Antigravity IDE / VS Code / Cursor /
              Windsurf / VS Code Insiders), the native installer
              (~/.local/bin), or npm (PATH)
    agy     ← Antigravity CLI installer (~/.local/bin), older installs
              (~/.gemini/bin), PATH, %LOCALAPPDATA%\\Programs\\Antigravity

Every copy found is ranked by version, so the newest one wins. Either, both,
or neither may be present; the server adapts to whatever is found. Missing
ones can be installed with `switchboard-ai setup` (see installer.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

HOME = Path.home()
LOCAL_BIN = HOME / ".local" / "bin"

_CLAUDE_EXT_DIRS = [
    HOME / ".antigravity-ide" / "extensions",
    HOME / ".antigravity" / "extensions",
    HOME / ".vscode" / "extensions",
    HOME / ".vscode-insiders" / "extensions",
    HOME / ".cursor" / "extensions",
    HOME / ".windsurf" / "extensions",
]

_EXE = ".exe" if os.name == "nt" else ""

# Version in the install path: IDE extension folder
# (anthropic.claude-code-2.1.286-linux-x64) or native installer
# (~/.local/share/claude/versions/2.1.286).
_VERSION_RE = re.compile(r"(?:claude-code-|[\\/]versions[\\/])(\d+(?:\.\d+)+)")


def _parse_version(text: str) -> tuple:
    return tuple(int(x) for x in text.split(".") if x.isdigit())


def claude_version(path: Path) -> tuple:
    """A claude binary's version, read from where it is installed (no process
    is started). (0,) when unknown, which ranks it last."""
    try:
        real = path.resolve()
    except OSError:
        return (0,)
    m = _VERSION_RE.search(str(real))
    if m:
        return _parse_version(m.group(1))
    # npm: node_modules/@anthropic-ai/claude-code/bin/claude.exe
    pkg = real.parent.parent / "package.json"
    try:
        return _parse_version(json.loads(pkg.read_text(encoding="utf-8")).get("version", "")) or (0,)
    except (OSError, ValueError, AttributeError):
        return (0,)


def _unique(paths: list[Path]) -> list[Path]:
    """Drop duplicates, including symlinks to the same file."""
    seen, out = set(), []
    for p in paths:
        try:
            key = str(p.resolve()).lower()
        except OSError:
            key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def find_claude_binaries() -> list[Path]:
    """Every claude binary found, newest version first."""
    found: list[Path] = []
    for ext_dir in _CLAUDE_EXT_DIRS:
        if ext_dir.is_dir():
            found.extend(
                ext_dir.glob(f"anthropic.claude-code-*/resources/native-binary/claude{_EXE}")
            )
    for p in (LOCAL_BIN / f"claude{_EXE}", HOME / ".claude" / "local" / f"claude{_EXE}"):
        if p.is_file():
            found.append(p)
    on_path = shutil.which("claude")
    if on_path:
        found.append(Path(on_path))

    # Stable sort: equal versions keep the order above (IDE copies first).
    return sorted(_unique(found), key=claude_version, reverse=True)


def find_agy_binaries() -> list[Path]:
    """Every agy binary found, preferred location first."""
    candidates = [LOCAL_BIN / f"agy{_EXE}", HOME / ".gemini" / "bin" / f"agy{_EXE}"]
    on_path = shutil.which("agy")
    if on_path:
        candidates.append(Path(on_path))
    local = os.getenv("LOCALAPPDATA")
    if local:
        for app in ("Antigravity", "Antigravity IDE"):
            root = Path(local) / "Programs" / app
            if root.is_dir():
                candidates.extend(root.glob(f"**/bin/agy{_EXE}"))
    return _unique([p for p in candidates if p.is_file()])


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
