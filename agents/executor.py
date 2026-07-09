"""
executor.py — Action Executor (Layer 5)
=======================================
Translates the model's decision into a precise, real action on the Mac.

The headline action is click_element(id): it looks up the REAL coordinate that
perception (ax_tree.py) already computed for that numbered element, and clicks
exactly there. No scaling, no guessing — that whole class of bug is gone.

Reuses the mature, battle-tested pieces from the legacy agent:
  • clipboard typing (safe for Arabic/Unicode)
  • guaranteed modifier-key release
  • destructive-action safety gate (here returns a signal; the orchestrator/
    Telegram bridge does the actual human confirmation)
"""

from __future__ import annotations

import os
import re
import time
import subprocess
from typing import Any, Callable, Optional

import pyautogui

pyautogui.FAILSAFE = True   # slam mouse to a corner to abort
pyautogui.PAUSE = 0.08

# ── Tunables ────────────────────────────────────────────────────────────────
MOVE_SETTLE_S = 0.05
CLIPBOARD_PASTE_DELAY_S = 0.10
KEY_CHORD_INTERVAL_S = 0.008
PAUSE_AFTER_CLICK_S = 0.5

# Words that mark an action as destructive → require confirmation upstream.
DESTRUCTIVE_KEYWORDS = (
    "delete", "remove", "trash", "erase", "purchase", "buy", "pay",
    "checkout", "confirm order", "send money", "transfer", "format disk",
    "empty trash", "uninstall", "wipe",
)


# ── App-name resolution (so open_app never guesses a wrong app) ──────────────
_APP_DIRS = (
    "/Applications", "/System/Applications",
    os.path.expanduser("~/Applications"), "/Applications/Utilities",
    "/System/Applications/Utilities",
)
_app_name_cache: Optional[dict] = None


def _list_app_names() -> dict:
    """Map lowercased app name → real display name, scanned from standard app
    directories. Cached on first use."""
    global _app_name_cache
    if _app_name_cache is not None:
        return _app_name_cache
    found: dict = {}
    for d in _APP_DIRS:
        try:
            for entry in os.listdir(d):
                if entry.endswith(".app"):
                    disp = entry[:-4]
                    found.setdefault(disp.lower(), disp)
        except OSError:
            pass
    _app_name_cache = found
    return found


def resolve_app_name(name: str) -> Optional[str]:
    """Resolve a (possibly casual/wrong) app name to a real installed app's
    display name — e.g. 'WhatsApp Desktop'/'WhatsApp Messenger' → 'WhatsApp'.
    Returns None if nothing plausible matches. Pure (unit-testable via _list_app_names)."""
    name = (name or "").strip().lower().replace(".app", "").strip()
    if not name:
        return None
    apps = _list_app_names()
    if name in apps:
        return apps[name]
    # Substring either way: 'whatsapp desktop' contains 'whatsapp'.
    for k, disp in apps.items():
        if name in k or k in name:
            return disp
    # Token overlap: every token of the request appears in an app name.
    toks = [t for t in re.findall(r"[a-z0-9]+", name) if t]
    if toks:
        for k, disp in apps.items():
            if all(t in k for t in toks):
                return disp
    return None


# ── Input primitives (ported from legacy, hardened) ─────────────────────────

def _move_and_settle(x: int, y: int) -> None:
    pyautogui.moveTo(x, y)
    time.sleep(MOVE_SETTLE_S)


def _type_via_clipboard(text: str) -> None:
    """
    Save clipboard → write text → verify → paste → restore. Robust for Unicode
    (Arabic included) where per-character typing breaks.
    """
    saved: Optional[bytes] = None
    try:
        saved = subprocess.run(["pbpaste"], capture_output=True, timeout=3).stdout
    except Exception:
        pass
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=3)
        verify = subprocess.run(["pbpaste"], capture_output=True, timeout=3)
        if verify.stdout.decode("utf-8", errors="replace") != text:
            # Clipboard didn't take; fall back to typing if ASCII-safe.
            if text.isascii():
                pyautogui.typewrite(text, interval=0.02)
            return
        pyautogui.hotkey("command", "v")
        time.sleep(CLIPBOARD_PASTE_DELAY_S)
    finally:
        if saved is not None:
            try:
                subprocess.run(["pbcopy"], input=saved, check=False, timeout=3)
            except Exception:
                pass


_KEY_MAP = {
    "command": "command", "cmd": "command", "win": "command",
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift", "fn": "fn",
    "enter": "return", "return": "return", "esc": "escape", "escape": "escape",
}


def _press_key_safe(key_str: str) -> None:
    """Press a key or chord with GUARANTEED modifier release in finally."""
    key_str = key_str.strip()
    if "+" not in key_str:
        pyautogui.press(_KEY_MAP.get(key_str.lower(), key_str.lower()))
        return
    parts = [p.strip() for p in key_str.split("+")]
    mods, final = parts[:-1], parts[-1]
    pressed: list[str] = []
    try:
        for m in mods:
            mapped = _KEY_MAP.get(m.lower(), m.lower())
            pyautogui.keyDown(mapped)
            pressed.append(mapped)
            time.sleep(KEY_CHORD_INTERVAL_S)
        pyautogui.press(_KEY_MAP.get(final.lower(), final.lower()))
    finally:
        for m in reversed(pressed):
            try:
                pyautogui.keyUp(m)
            except Exception:
                pass


SCREENSHOT_PATH = "/tmp/_agent_screen.png"

def _capture_screen_file() -> bool:
    """Capture the screen to SCREENSHOT_PATH (works even when AGENT_SCREENSHOTS=0)."""
    try:
        subprocess.run(["screencapture", "-x", "-t", "png", SCREENSHOT_PATH],
                       capture_output=True, timeout=10, check=True)
        return os.path.isfile(SCREENSHOT_PATH)
    except Exception:
        return False



class ActionResult:
    """Outcome of executing one action."""

    def __init__(
        self,
        ok: bool,
        message: str,
        *,
        terminal: Optional[str] = None,   # "done" | "failed" | None
        clicked_xy: Optional[tuple[int, int]] = None,
        needs_confirmation: bool = False,
        question: Optional[str] = None,
        deliver_screenshot: bool = False,
    ):
        self.ok = ok
        self.message = message
        self.terminal = terminal
        self.clicked_xy = clicked_xy
        self.needs_confirmation = needs_confirmation
        self.question = question
        self.deliver_screenshot = deliver_screenshot

    def __repr__(self) -> str:
        return f"<ActionResult ok={self.ok} '{self.message[:50]}'>"


# ── The executor ────────────────────────────────────────────────────────────

class Executor:
    """
    Stateless executor. Pass it the current numbered element list each turn so
    click_element can resolve a number → real coordinate.

    `confirm_cb`: optional callable(question:str) -> bool used to confirm
    destructive actions. If None, destructive actions are flagged via
    ActionResult.needs_confirmation for the caller (Telegram) to handle.
    """

    def __init__(self, confirm_cb: Optional[Callable[[str], bool]] = None):
        self.confirm_cb = confirm_cb

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _is_destructive(action: dict, element: Optional[dict] = None) -> bool:
        values = [action.get(k, "") for k in
                  ("thought", "text", "question", "summary")]
        if element is not None:
            # The model's prose can be neutral even when the selected control is
            # consequential (for example: thought="click the button", name="Delete").
            # Include the observed AX metadata so the safety decision is based on
            # the target as well as the model-supplied explanation.
            values.extend(element.get(k, "") for k in
                          ("name", "title", "label", "description", "value", "help", "role"))
        blob = " ".join(str(value) for value in values).lower()
        return any(kw in blob for kw in DESTRUCTIVE_KEYWORDS)

    # -- main entry -------------------------------------------------------
    def execute(self, action: dict, elements: list[dict]) -> ActionResult:
        act = str(action.get("action", "")).lower().strip()

        # Resolve the selected AX element before the gate so its observed label
        # and metadata participate in destructive-action detection.
        target_element: Optional[dict] = None
        if act in ("click_element", "double_click_element"):
            eid = action.get("element_id")
            target_element = next((e for e in elements if e.get("id") == eid), None)
            if target_element is None:
                return ActionResult(
                    False,
                    f"Element {eid} not in list. Pick a valid number, scroll, or wait.",
                )

        # Heuristic safety gate for destructive-looking actions. It is a useful
        # confirmation layer, not a standalone security boundary.
        if self._is_destructive(action, target_element) and act in (
            "click_element", "double_click_element", "press_key", "type"
        ):
            detail = str(action.get("thought") or act)
            if target_element is not None:
                detail += f" Target: [{target_element.get('id')}] {target_element.get('name', '(unnamed)')}"
            q = f"This looks destructive: {detail}. Proceed?"
            if self.confirm_cb is not None:
                if not self.confirm_cb(q):
                    return ActionResult(False, "Cancelled by user (destructive).")
            else:
                return ActionResult(
                    False, "Awaiting confirmation (destructive).",
                    needs_confirmation=True, question=q,
                )

        # -- click by element number (the main path) ----------------------
        if act in ("click_element", "double_click_element"):
            eid = action.get("element_id")
            assert target_element is not None  # resolved before the safety gate
            el = target_element
            x, y = int(el["x"]), int(el["y"])
            _move_and_settle(x, y)
            if act == "double_click_element":
                pyautogui.doubleClick(x, y)
                verb = "Double-clicked"
            else:
                pyautogui.click(x, y)
                verb = "Clicked"
            time.sleep(PAUSE_AFTER_CLICK_S)
            return ActionResult(
                True, f"{verb} [{eid}] \"{el['name']}\" ({el['role']})",
                clicked_xy=(x, y),
            )

        # -- launch / activate an app by REAL name (resolves guesses; Spotlight
        #    fallback so a slightly-wrong name still opens the right app) --------
        if act == "open_app":
            app_name = (action.get("app") or action.get("app_name")
                        or action.get("name") or "").strip()
            if not app_name:
                return ActionResult(False, "open_app without an app name")
            resolved = resolve_app_name(app_name)
            via = ""
            opened = False
            if resolved:
                try:
                    subprocess.run(["open", "-a", resolved], check=True,
                                   capture_output=True, timeout=10)
                    opened = True
                    if resolved.lower() != app_name.lower():
                        via = f" (resolved '{app_name}' → '{resolved}')"
                except Exception:
                    opened = False
            if not opened:
                # Spotlight fallback: cmd+space → type → return. Opens apps that
                # aren't in standard dirs or whose name couldn't be resolved. This
                # is what stops the agent from guessing a wrong app / opening
                # System Settings when it meant to "use Spotlight".
                _press_key_safe("command+space")
                time.sleep(0.6)
                _type_via_clipboard(app_name)
                time.sleep(0.3)
                _press_key_safe("return")
                time.sleep(1.8)  # let Spotlight's top result launch
                via = (via + " via Spotlight").strip()
                opened = True
            # `open`/Spotlight launch but macOS focus-stealing can leave the app
            # behind; force it frontmost so perception sees its AX tree.
            target = resolved or app_name
            esc = target.replace('"', '\\"')
            try:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{esc}" to activate'],
                    capture_output=True, timeout=5)
                time.sleep(0.6)
            except Exception:
                pass
            return ActionResult(True, f"Opened app: {target}{via}")

        # -- type into focused field --------------------------------------
        if act == "type":
            text = action.get("text", "")
            _type_via_clipboard(text)
            shown = text[:60] + ("…" if len(text) > 60 else "")
            return ActionResult(True, f"Typed: \"{shown}\"")

        # -- key / chord --------------------------------------------------
        if act == "press_key":
            key = action.get("key", "")
            if not key:
                return ActionResult(False, "press_key without a key")
            _press_key_safe(key)
            # App-launch / navigation keys benefit from a longer settle.
            if key in ("return", "command+space") or "space" in key:
                time.sleep(2.0)
            return ActionResult(True, f"Pressed: {key}")

        # -- scroll -------------------------------------------------------
        if act == "scroll":
            direction = action.get("direction", "down").lower()
            amount = int(action.get("amount", 3))
            clicks = -amount if direction == "down" else amount
            pyautogui.scroll(clicks)
            time.sleep(0.3)
            return ActionResult(True, f"Scrolled {direction} x{amount}")

        # -- wait ---------------------------------------------------------
        if act == "wait":
            dur = float(action.get("duration", 2))
            time.sleep(dur)
            return ActionResult(True, f"Waited {dur:g}s")

        if act == "send_screenshot":
            if _capture_screen_file():
                return ActionResult(True, "Screenshot captured and sent to user",
                                    deliver_screenshot=True)
            return ActionResult(False, "Screenshot capture failed")

        # -- ask the user -------------------------------------------------
        if act == "ask_user":
            q = action.get("question", "(no question)")
            return ActionResult(
                True, f"Asking user: {q}", question=q, needs_confirmation=False,
            )

        # -- terminal states ----------------------------------------------
        if act == "done":
            return ActionResult(True, action.get("summary", "Task complete"), terminal="done")
        if act == "failed":
            return ActionResult(False, action.get("summary", "Task failed"), terminal="failed")

        return ActionResult(False, f"Unknown action: {act}")
