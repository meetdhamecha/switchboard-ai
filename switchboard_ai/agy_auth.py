"""
switchboard_ai.agy_auth
───────────────────
Sign the Antigravity CLI (agy) in and out from the web UI.

agy keeps its login in the OS keyring and has no login / logout subcommands:
both live only in its interactive UI. That UI runs here in a pseudo-terminal
(Linux / macOS) and is read through a terminal emulator (pyte):

  - Sign in: choose "Google OAuth" and hand agy's Google sign-in link to the
    browser. Google then passes the login back to agy, or shows a code that
    the user pastes into the web UI and that is typed into agy.
  - Log out: type /logout.
  - Before its UI opens the first time, agy shows a one-time setup (color
    scheme, then Google's terms and a data-sharing choice). The web UI shows
    those terms; only after the user agrees there does setup() finish agy's
    setup with the data-sharing choice they made.

Whether agy is signed in comes from `agy models`, which only lists models
for a signed-in account. It takes a few seconds, so the answer is cached.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from typing import Optional

_STATUS_TTL = 60.0
_LOGIN_TIMEOUT = 600
_LOGIN_CHECK_EVERY = 8.0   # seconds between "signed in yet?" checks while signing in
_CONFIRM = re.compile(r"(?i)are you sure|confirm|\(y/n\)|\[y/n\]")
_CODE_RE = re.compile(r"^[A-Za-z0-9/_.~+=-]{4,512}$")

# agy's first-run setup screens.
_SCHEME = "Choose your color scheme"
_TERMS = "Terms of Service & Data Use"
_DATA_OPT_IN = "Yes, I agree to help improve"
# Folder-trust question for the working folder, and the prompt once agy is ready.
_TRUST = "Do you trust the contents of this project?"
_TRUST_YES = "> Yes, I trust this folder"
_READY = "? for shortcuts"
# Sign-in screens.
_LOGIN_METHOD = "Select login method"
_LOGIN_GOOGLE = "> 1. Google OAuth"
_LOGIN_URL = "https://accounts.google.com/"
# Every screen agy can open on.
_START_SCREENS = (_SCHEME, _TERMS, _LOGIN_METHOD, _TRUST, _READY)
_ALREADY = "agy is already signed in. Log out first to use another account."
# agy logs this as soon as it holds a valid login.
_SIGNED_IN_LOG = "OAuth: authenticated successfully"

_ENTER, _DOWN, _RIGHT = b"\r", b"\x1b[B", b"\x1b[C"


def _workdir() -> str:
    path = os.path.join(tempfile.gettempdir(), "switchboard-agy-auth")
    os.makedirs(path, exist_ok=True)
    return path


# Commands agy may use to open a browser (Linux desktops, WSL, macOS).
_BROWSER_OPENERS = ("xdg-open", "gio", "sensible-browser", "x-www-browser", "www-browser",
                    "gnome-open", "kde-open", "kde-open5", "wslview", "open")


def _no_browser_dir() -> str:
    """A folder of do-nothing stand-ins for the browser openers. Put first on
    agy's PATH so it can't open its own tab next to the one the web UI opens.
    (Hiding DISPLAY is not enough: xdg-open also works over D-Bus, which agy
    needs for the keyring.)"""
    path = os.path.join(_workdir(), "no-browser")
    os.makedirs(path, exist_ok=True)
    for name in _BROWSER_OPENERS:
        shim = os.path.join(path, name)
        if not os.path.exists(shim):
            with open(shim, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(shim, 0o755)
    return path


class AgyAuth:
    def __init__(self) -> None:
        self._signed_in: Optional[bool] = None
        self._checked = 0.0
        self._check_lock = asyncio.Lock()
        self._busy = asyncio.Lock()      # logout / setup, one at a time
        self._login: Optional[_LoginSession] = None
        self._bg: Optional[asyncio.Task] = None

    def _remember(self, signed_in: bool) -> None:
        self._signed_in, self._checked = signed_in, time.monotonic()

    def _reset_in_background(self, provider) -> None:
        """Restart agy processes and reload models without making the user wait."""
        self._bg = asyncio.create_task(provider.reset_sessions())

    @property
    def cached_signed_in(self) -> Optional[bool]:
        return self._signed_in

    def _login_busy(self) -> bool:
        return bool(self._login and self._login.state in ("starting", "waiting"))

    # ── status ──────────────────────────────────────────────

    async def check(self, binary: str, refresh: bool = False) -> Optional[bool]:
        """True / False, or None when agy did not answer."""
        async with self._check_lock:
            if not refresh and self._signed_in is not None and time.monotonic() - self._checked < _STATUS_TTL:
                return self._signed_in
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary, "models", cwd=_workdir(),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    stdin=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
            except (OSError, asyncio.TimeoutError):
                return None
            self._signed_in = any("\t" in line for line in out.decode("utf-8", "replace").splitlines())
            self._checked = time.monotonic()
            return self._signed_in

    async def status(self, provider, refresh: bool = False) -> dict:
        login = self._login
        if login and login.state == "done" and not login.finalized:
            # The sign-in thread can't await: refresh state for the new account here.
            login.finalized = True
            self._remember(True)
            self._reset_in_background(provider)
        return {
            "available": True,
            "signed_in": await self.check(provider.binary, refresh),
            "login": login.snapshot() if login else {"state": "idle", "url": None, "error": None},
            "supported": os.name != "nt",
        }

    # ── sign in ─────────────────────────────────────────────

    async def start_login(self, provider) -> dict:
        if os.name == "nt":
            return {"ok": False, "error": "On Windows, run agy in a terminal to sign in."}
        if self._login_busy():
            return {"ok": True, "login": self._login.snapshot()}
        if self._busy.locked():
            return {"ok": False, "error": "agy is busy logging out. Try again in a moment."}
        # A known signed-out state needs no slow check; agy's own screen
        # tells the session if it is signed in after all.
        if self._signed_in is not False and await self.check(provider.binary, refresh=True):
            return {"ok": False, "already": True, "error": _ALREADY}
        session = _LoginSession(provider.binary)
        self._login = session
        threading.Thread(target=session.run, daemon=True).start()
        deadline = time.monotonic() + 40  # agy start-up + sign-in screen
        while session.state == "starting" and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        if session.state == "failed" and session.needs_setup:
            return {"ok": False, "needs_setup": True, "error": session.error}
        if session.state == "failed" and session.already:
            self._remember(True)
            return {"ok": False, "already": True, "error": session.error}
        if session.state == "failed":
            return {"ok": False, "error": session.error}
        return {"ok": True, "login": session.snapshot()}

    def submit_code(self, code: str) -> dict:
        code = code.strip()
        if not self._login_busy():
            return {"ok": False, "error": "No sign-in in progress."}
        if not _CODE_RE.match(code):
            return {"ok": False, "error": "That doesn't look like an authorization code."}
        self._login.codes.put(code)
        return {"ok": True}

    def cancel_login(self) -> dict:
        if self._login_busy():
            self._login.cancel.set()
        return {"ok": True}

    # ── sign out ────────────────────────────────────────────

    async def logout(self, provider) -> dict:
        if os.name == "nt":
            return {"ok": False, "error": "On Windows, run agy in a terminal and type /logout."}
        if self._login_busy():
            return {"ok": False, "error": "A sign-in is in progress. Cancel it first."}
        async with self._busy:
            await provider.shutdown()  # warm agy processes still hold the old account
            try:
                result, screen = await asyncio.to_thread(_run_logout, provider.binary)
                if result == "setup":
                    return {"ok": False, "needs_setup": True,
                            "error": "agy needs its one-time setup before it can log out."}
                if result == "unexpected":
                    print(f"  [antigravity] agy showed an unexpected screen:\n{screen}")
                    return {"ok": False, "error": "agy showed an unexpected screen. See the server log."}
                if result == "signed_out":  # agy is back on its sign-in screen
                    self._remember(False)
                    signed_in = False
                else:
                    signed_in = await self.check(provider.binary, refresh=True)
            finally:
                self._reset_in_background(provider)
        if signed_in is False:
            self._login = None
            return {"ok": True}
        print(f"  [antigravity] /logout did not sign agy out. Last screen:\n{screen}")
        return {"ok": False, "error": "agy is still signed in. See the server log."}

    async def setup(self, provider, share_data: bool) -> dict:
        """Finish agy's first-run setup after the user agreed to the terms in
        the web UI: keep the default color scheme, set the data-sharing choice
        they made, then confirm."""
        if os.name == "nt":
            return {"ok": False, "error": "On Windows, run agy once in a terminal to finish its setup."}
        if self._login_busy():
            return {"ok": False, "error": "A sign-in is in progress. Cancel it first."}
        async with self._busy:
            error = await asyncio.to_thread(_run_setup, provider.binary, share_data)
        return {"ok": not error, "error": error}


class _LoginSession:
    """One sign-in, run in a thread: agy's UI stays open until the user has
    signed in with Google (or pasted the code Google shows), then closes."""

    def __init__(self, binary: str) -> None:
        self.binary = binary
        self.state = "starting"          # starting | waiting | done | failed
        self.url: Optional[str] = None
        self.error: Optional[str] = None
        self.needs_setup = False
        self.already = False
        self.finalized = False
        self.codes: "queue.Queue[str]" = queue.Queue()
        self.cancel = threading.Event()
        # agy's log for this session only: it says the moment sign-in succeeds.
        self.log_path = os.path.join(_workdir(), f"login-{os.getpid()}-{threading.get_ident()}.log")

    def _log_says_signed_in(self) -> bool:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return _SIGNED_IN_LOG in f.read()
        except OSError:
            return False

    def snapshot(self) -> dict:
        return {"state": self.state, "url": self.url, "error": self.error}

    def _fail(self, error: str, screen: str = "") -> None:
        if screen:
            print(f"  [antigravity] sign-in stopped: {error}\n{screen}")
        self.error, self.state = error, "failed"

    def run(self) -> None:
        try:
            # No display: agy must not open a browser on the server; the web
            # UI opens the sign-in link in the user's own browser instead.
            term = _Terminal(self.binary, headless=True, args=("--log-file", self.log_path))
        except Exception as e:
            return self._fail(str(e))
        try:
            seen = term.wait_for(*_START_SCREENS, timeout=30)
            if seen in (_SCHEME, _TERMS):
                self.needs_setup = True
                return self._fail("agy needs its one-time setup before it can sign in.")
            if seen == _TRUST:
                if not _answer_trust(term):
                    return self._fail("agy asked an unexpected question.", _redact(term.text()))
                seen = term.wait_for(_LOGIN_METHOD, _READY, timeout=20)
            if seen == _READY:
                self.already = True
                return self._fail(_ALREADY)
            if seen != _LOGIN_METHOD or _LOGIN_GOOGLE not in term.text():
                return self._fail("agy didn't show its sign-in screen.", _redact(term.text()))
            term.press(_ENTER, 0.3)  # 1. Google OAuth
            if term.wait_for(_LOGIN_URL, timeout=10):
                term.pump(0.5)  # let the whole wrapped link draw
            self.url = _sign_in_url(term.text())
            if not self.url:
                return self._fail("agy didn't show a Google sign-in link.", _redact(term.text()))
            self.state = "waiting"

            deadline = time.monotonic() + _LOGIN_TIMEOUT
            next_check = time.monotonic() + _LOGIN_CHECK_EVERY
            while time.monotonic() < deadline:
                if self.cancel.is_set():
                    return self._fail("Sign-in cancelled.")
                try:
                    code = self.codes.get_nowait()
                    term.paste(code)
                    term.press(_ENTER, 0.3)
                except queue.Empty:
                    term.pump(0.5)
                if self._log_says_signed_in():
                    self.state = "done"
                    return
                if term.exited:
                    if _models_signed_in(self.binary):
                        self.state = "done"
                        return
                    return self._fail("agy closed before sign-in finished.")
                if _TRUST in term.text() and not _answer_trust(term):
                    return self._fail("agy asked an unexpected question.", _redact(term.text()))
                if _READY in term.text():
                    self.state = "done"
                    return
                # After sign-in agy may show screens not handled above; the
                # keyring login is what counts, so ask agy directly as well.
                if time.monotonic() >= next_check:
                    if _models_signed_in(self.binary):
                        print(f"  [antigravity] signed in; agy was showing:\n{_redact(term.text())}")
                        self.state = "done"
                        return
                    next_check = time.monotonic() + _LOGIN_CHECK_EVERY
            self._fail("Sign-in timed out.")
        except Exception as e:
            self._fail(f"Sign-in failed: {e}")
        finally:
            term.close()
            try:
                os.remove(self.log_path)  # holds the account's email
            except OSError:
                pass


def _models_signed_in(binary: str) -> bool:
    """`agy models` lists models only for a signed-in account."""
    try:
        out = subprocess.run([binary, "models"], cwd=_workdir(), capture_output=True,
                             text=True, timeout=45, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any("\t" in line for line in out.splitlines())


def _sign_in_url(text: str) -> Optional[str]:
    """agy wraps the long Google link over several screen lines; rejoin it."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(_LOGIN_URL):
            parts = []
            for part in lines[i:]:
                if not part.strip():
                    break
                parts.append(part.strip())
            url = "".join(parts)
            return url if re.fullmatch(r"https://accounts\.google\.com/\S+", url) else None
    return None


def _answer_trust(term: "_Terminal") -> bool:
    """Trust the working folder, which is Switchboard's own empty scratch
    folder (_workdir), so agy gets access to nothing else."""
    text = term.text()
    if _TRUST_YES not in text or _workdir() not in text:
        return False
    term.press(_ENTER, 0.3)
    return True


class _Terminal:
    """agy's interactive UI in a pseudo-terminal, rendered by a terminal
    emulator so screens can be recognised by their text."""

    COLS, ROWS = 120, 45

    def __init__(self, binary: str, headless: bool = False, args: tuple[str, ...] = ()) -> None:
        import fcntl
        import pty
        import struct
        import termios

        try:
            import pyte
        except ImportError:
            raise RuntimeError("The pyte package is missing: pip install pyte") from None
        self.exited = False
        no_browser = _no_browser_dir() if headless else ""
        self.screen = pyte.Screen(self.COLS, self.ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child: never return into the server's code
            try:
                os.environ["TERM"] = "xterm-256color"
                if headless:
                    for var in ("DISPLAY", "WAYLAND_DISPLAY"):
                        os.environ.pop(var, None)
                    os.environ["BROWSER"] = "true"
                    os.environ["PATH"] = no_browser + os.pathsep + os.environ.get("PATH", "")
                os.chdir(_workdir())
                os.execv(binary, [binary, *args])
            finally:
                os._exit(127)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", self.ROWS, self.COLS, 0, 0))

    def pump(self, seconds: float, quiet: float = 0.0) -> None:
        """Read output for up to `seconds`, answering terminal queries; with
        `quiet`, stop early once nothing has arrived for that long."""
        import select

        end = time.monotonic() + seconds
        last, seen = time.monotonic(), False
        while time.monotonic() < end:
            if quiet and seen and time.monotonic() - last > quiet:
                return
            ready, _, _ = select.select([self.fd], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:  # agy exited
                self.exited = True
                return
            last, seen = time.monotonic(), True
            for query, reply in _REPLIES:
                if query in chunk:
                    os.write(self.fd, reply)
            # Kitty graphics probes are not understood by pyte.
            self.stream.feed(re.sub(rb"\x1b_[^\x1b]*\x1b\\", b"", chunk))

    def wait_for(self, *markers: str, timeout: float = 20.0) -> Optional[str]:
        """Read output until one of `markers` is on screen; returns that
        marker, or None on timeout or if agy exits."""
        end = time.monotonic() + timeout
        while True:
            text = self.text()
            for marker in markers:
                if marker in text:
                    return marker
            if self.exited or time.monotonic() >= end:
                return None
            self.pump(0.25)

    def text(self) -> str:
        return "\n".join(line.rstrip() for line in self.screen.display)

    def press(self, key: bytes, wait: float = 1.5) -> None:
        os.write(self.fd, key)
        self.pump(wait)

    def paste(self, text: str) -> None:
        os.write(self.fd, text.encode())
        self.pump(0.2)

    def type(self, text: str) -> None:
        for ch in text:
            os.write(self.fd, ch.encode())
            time.sleep(0.02)
        self.pump(0.3)

    def focused(self, label: str) -> bool:
        """Whether `label` is drawn highlighted (agy's focused button)."""
        for y in range(self.ROWS):
            row = self.screen.buffer[y]
            line = "".join(row[x].data for x in range(self.COLS))
            x = line.find(label)
            if x >= 0 and all(row[i].reverse for i in range(x, x + len(label))):
                return True
        return False

    def in_setup(self) -> bool:
        text = self.text()
        return _SCHEME in text or _TERMS in text

    def close(self) -> None:
        import signal

        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                break
            time.sleep(0.3)
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        os.close(self.fd)


# Terminal queries agy sends at startup, and the answers it waits for.
_REPLIES = (
    (b"\x1b[c", b"\x1b[?62;22c"),
    (b"\x1b[0c", b"\x1b[?62;22c"),
    (b"\x1b[6n", b"\x1b[1;1R"),
    (b"\x1b[?u", b"\x1b[?0u"),
    (b"\x1b]10;?", b"\x1b]10;rgb:ffff/ffff/ffff\x07"),
    (b"\x1b]11;?", b"\x1b]11;rgb:0000/0000/0000\x07"),
)


def _redact(text: str) -> str:
    return re.sub(r"\S+@\S+\.\S+", "<email>", text)


def _open_prompt(term: _Terminal) -> Optional[str]:
    """Wait until agy's prompt is ready. Returns None, "setup" when the
    first-run setup is showing, "signed_out", or "unexpected"."""
    seen = term.wait_for(*_START_SCREENS, timeout=30)
    if seen in (_SCHEME, _TERMS):
        return "setup"
    if seen == _TRUST:
        if not _answer_trust(term):
            return "unexpected"
        seen = term.wait_for(_LOGIN_METHOD, _READY, timeout=20)
    if seen == _LOGIN_METHOD:
        return "signed_out"
    return None if seen == _READY else "unexpected"


def _run_logout(binary: str) -> tuple[str, str]:
    """Type /logout into agy's UI. Returns ("done" | "setup" | "signed_out" |
    "unexpected", last screen)."""
    term = _Terminal(binary)
    try:
        problem = _open_prompt(term)
        if problem:
            return problem, _redact(term.text())
        term.type("/logout")
        term.press(_ENTER, 0.3)
        seen = term.wait_for(_LOGIN_METHOD, timeout=8)
        if not seen and not term.exited and _CONFIRM.search(term.text()):
            term.press(_ENTER, 0.3)
            seen = term.wait_for(_LOGIN_METHOD, timeout=8)
        return ("signed_out" if seen else "done"), _redact(term.text())
    finally:
        term.close()


def _run_setup(binary: str, share_data: bool) -> Optional[str]:
    """Walk agy's first-run setup. Returns None on success, else an error."""
    term = _Terminal(binary)
    try:
        seen = term.wait_for(*_START_SCREENS, timeout=30)
        if seen not in (_SCHEME, _TERMS):
            return None  # already done
        if seen == _SCHEME:
            term.press(_ENTER, 0.3)  # keep the default color scheme
            term.wait_for(_TERMS, timeout=10)
        if _TERMS not in term.text():
            return "agy showed an unexpected setup screen. Run `agy` in a terminal to finish it."
        if (f"[x] {_DATA_OPT_IN}" in term.text()) != share_data:
            term.press(_ENTER, 0.8)  # toggle the data-sharing checkbox
            if (f"[x] {_DATA_OPT_IN}" in term.text()) != share_data:
                return "Could not set the data-sharing choice. Run `agy` in a terminal to finish setup."
        term.press(_DOWN, 0.8)
        term.press(_RIGHT, 0.8)
        if not term.focused("Done"):
            return "Could not reach the Done button. Run `agy` in a terminal to finish setup."
        term.press(_ENTER, 0.3)
        term.wait_for(_TRUST, _READY, _LOGIN_METHOD, timeout=15)
        if term.in_setup():
            return "agy's setup did not finish. Run `agy` in a terminal to finish it."
        return None
    finally:
        term.close()


agy_auth = AgyAuth()
