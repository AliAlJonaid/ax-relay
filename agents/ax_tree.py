"""
ax_tree.py — Deep macOS Accessibility Tree (Layer 3a)
=====================================================
The KEYSTONE of the agent. Walks the real macOS accessibility tree recursively
via the AXUIElement C API (through PyObjC) and returns a NUMBERED list of
clickable/typeable elements, each with its REAL on-screen coordinate from the OS.

Core principle: the model never guesses coordinates. It picks an element NUMBER.
This module produces that numbered list. Coordinates come from the OS, not a model.

Key features vs. the old shallow AppleScript approach:
  • Recursive traversal (deep), not just front-window buttons/textfields.
  • Reads role, title/description/value, position, size, and supported actions.
  • Enables browser web-content accessibility (AXManualAccessibility / AXEnhancedUserInterface)
    so Chrome/Safari page elements actually show up in the tree.
  • Returns screen coordinates ready for a precise click — zero scaling guesswork.

Requires (already installed in this project's venv):
  pyobjc-framework-ApplicationServices, pyobjc-framework-Cocoa
And the terminal/host app must have Accessibility permission
  (System Settings → Privacy & Security → Accessibility).
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

# ── PyObjC bridges to the Accessibility C API ───────────────────────────────
# ApplicationServices exposes the AX* functions; AppKit gives us NSWorkspace.
from ApplicationServices import (
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementCopyAttributeValue,
    AXUIElementCopyAttributeNames,
    AXUIElementCopyActionNames,
    AXUIElementSetAttributeValue,
    AXValueGetValue,
    kAXErrorSuccess,
    kAXChildrenAttribute,
    kAXRoleAttribute,
    kAXSubroleAttribute,
    kAXTitleAttribute,
    kAXDescriptionAttribute,
    kAXValueAttribute,
    kAXPlaceholderValueAttribute,
    kAXPositionAttribute,
    kAXSizeAttribute,
    kAXEnabledAttribute,
    kAXFocusedAttribute,
    kAXWindowsAttribute,
    kAXMainWindowAttribute,
    kAXValueTypeCGPoint,
    kAXValueTypeCGSize,
)
from AppKit import NSWorkspace, NSRunningApplication
from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGWindowListOptionOnScreenOnly,
    kCGNullWindowID,
)

# ── Configuration ───────────────────────────────────────────────────────────

# How deep to walk. Browser DOM can be deep, but going too deep is slow.
MAX_DEPTH = 28
# Hard cap on number of elements returned to the model (keep the prompt tight).
MAX_ELEMENTS = 60
# Per-app traversal time budget (seconds). AX calls can hang on some apps.
TRAVERSAL_BUDGET_S = 3.5

# Roles we consider INTERACTIVE (clickable or typeable). This is the allow-list
# that decides what gets a number. Everything else is structural and skipped.
INTERACTIVE_ROLES = {
    "AXButton",
    "AXMenuButton",
    "AXPopUpButton",
    "AXMenuItem",
    "AXMenuBarItem",
    "AXTextField",
    "AXTextArea",
    "AXSearchField",
    "AXSecureTextField",
    "AXComboBox",
    "AXCheckBox",
    "AXRadioButton",
    "AXLink",
    "AXTabButton",
    "AXDisclosureTriangle",
    "AXSlider",
    "AXIncrementor",
    "AXSegmentedControl",
    "AXToggle",
}

# Containers we ALWAYS descend into even if not interactive (perf: avoid walking
# everything blindly). Empty here means "descend into all containers"; we keep a
# curated set so traversal stays fast and focused on real UI.
CONTAINER_ROLES = {
    "AXApplication", "AXWindow", "AXGroup", "AXToolbar", "AXScrollArea",
    "AXSplitGroup", "AXTabGroup", "AXList", "AXTable", "AXOutline", "AXRow",
    "AXCell", "AXWebArea", "AXScrollBar", "AXGenericElement", "AXBox",
    "AXLayoutArea", "AXLayoutItem", "AXMenu", "AXMenuBar", "AXSheet",
    "AXDrawer", "AXPopover", "AXUnknown", "AXStaticText",
}

# List/sidebar containers whose text rows behave as clickable navigation even
# though the labelled leaf only exposes AXShowMenu (not AXPress) — e.g. the
# System Settings sidebar (AXOutline > AXRow > AXCell > AXStaticText).
LIST_CONTAINER_ROLES = {"AXOutline", "AXList", "AXTable"}
# Otherwise-structural roles we PROMOTE to clickable when they sit inside one of
# the LIST_CONTAINER_ROLES above, so nav items become numbered and selectable.
NAVROW_ROLES = {"AXStaticText", "AXCell", "AXRow"}

# Modal roles. When one of these is open it captures ALL interaction; the window
# behind it is occluded. We must traverse ONLY the modal, otherwise we'd surface
# (and the model would click) background elements that are now hidden behind the
# modal — the click lands on the modal overlay instead = "pressed the wrong spot".
MODAL_ROLES = {"AXSheet", "AXPopover"}

# Browser bundle IDs that need web-content accessibility turned on explicitly.
_BROWSER_BUNDLES = {
    "com.google.Chrome", "com.google.Chrome.canary", "com.brave.Browser",
    "com.microsoft.edgemac", "com.vivaldi.Vivaldi", "company.thebrowser.Browser",
    "org.chromium.Chromium",
}
# Safari uses a slightly different path but the same attribute trick helps.
_SAFARI_BUNDLES = {"com.apple.Safari", "com.apple.SafariTechnologyPreview"}


# ── Low-level AX helpers ────────────────────────────────────────────────────

def _copy_attr(element: Any, attr: str) -> Optional[Any]:
    """Safe AXUIElementCopyAttributeValue → returns the value or None."""
    try:
        err, value = AXUIElementCopyAttributeValue(element, attr, None)
        if err == kAXErrorSuccess:
            return value
    except Exception:
        pass
    return None


def _attr_names(element: Any) -> list[str]:
    try:
        err, names = AXUIElementCopyAttributeNames(element, None)
        if err == kAXErrorSuccess and names:
            return list(names)
    except Exception:
        pass
    return []


def _action_names(element: Any) -> list[str]:
    try:
        err, names = AXUIElementCopyActionNames(element, None)
        if err == kAXErrorSuccess and names:
            return list(names)
    except Exception:
        pass
    return []


def _parse_axvalue_text(raw: Any) -> Optional[tuple[float, float]]:
    """
    Fallback extractor: some PyObjC versions don't cleanly support AXValueGetValue
    for CGPoint/CGSize. The repr of an AXValue reliably contains the numbers, e.g.
        "<AXValue 0x... {value = x:412.000000 y:88.000000 type = kAXValueCGPointType}>"
        "<AXValue 0x... {value = w:120.000000 h:32.000000 ...}>"
    We pull the two floats out of that text. This is the approach proven in pyatom.
    """
    try:
        s = repr(raw) + " " + str(raw)
    except Exception:
        return None
    nums = re.findall(r"[-+]?\d+\.?\d*", s)
    # The repr includes a hex pointer; restrict to the {value = ...} segment.
    seg = re.search(r"\{value\s*=\s*([^}]*)\}", s)
    if seg:
        nums = re.findall(r"[-+]?\d+\.?\d*", seg.group(1))
    if len(nums) >= 2:
        try:
            return (float(nums[0]), float(nums[1]))
        except ValueError:
            return None
    return None


def _get_point(element: Any, attr: str) -> Optional[tuple[float, float]]:
    """Extract a CGPoint AXValue (e.g. position) as (x, y)."""
    raw = _copy_attr(element, attr)
    if raw is None:
        return None
    # Primary path: native AXValueGetValue (works on modern PyObjC, incl. 12.x).
    try:
        ok, pt = AXValueGetValue(raw, kAXValueTypeCGPoint, None)
        if ok and pt is not None:
            return (float(pt.x), float(pt.y))
    except Exception:
        pass
    # Fallback path: parse the numbers out of the AXValue text.
    return _parse_axvalue_text(raw)


def _get_size(element: Any, attr: str) -> Optional[tuple[float, float]]:
    """Extract a CGSize AXValue (e.g. size) as (w, h)."""
    raw = _copy_attr(element, attr)
    if raw is None:
        return None
    try:
        ok, sz = AXValueGetValue(raw, kAXValueTypeCGSize, None)
        if ok and sz is not None:
            return (float(sz.width), float(sz.height))
    except Exception:
        pass
    return _parse_axvalue_text(raw)


def _str(value: Any) -> str:
    """Coerce an AX attribute value to a clean short string."""
    if value is None:
        return ""
    try:
        s = str(value).strip()
    except Exception:
        return ""
    # Collapse whitespace/newlines that bloat the prompt.
    s = " ".join(s.split())
    return s[:80]


# ── Frontmost app discovery ─────────────────────────────────────────────────

def _live_frontmost_pid_name() -> Optional[tuple[int, str]]:
    """
    Live (pid, owner_name) of the frontmost app via the on-screen window list.

    We must NOT use NSWorkspace.frontmostApplication() as the source of truth:
    in a long-lived process that never pumps a run loop (exactly our agent loop,
    and the Telegram worker thread), that property is FROZEN at the value it had
    when the process started, because the app-activation notifications that would
    update it are never delivered. The window list, by contrast, is queried live
    every call — so it correctly tracks the frontmost app after the agent switches
    apps (the bug behind every stale "App: Claude" read).
    """
    try:
        wins = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    except Exception:
        return None
    if not wins:
        return None
    for w in wins:  # ordered front-to-back
        # Layer 0 = normal application windows; skip the menu bar / overlays.
        if w.get("kCGWindowLayer", 99) == 0:
            pid = w.get("kCGWindowOwnerPID")
            if pid:
                return int(pid), str(w.get("kCGWindowOwnerName") or "")
    return None


def get_frontmost_app() -> tuple[Optional[int], str, str]:
    """Return (pid, app_name, bundle_id) of the frontmost application (LIVE)."""
    ws = NSWorkspace.sharedWorkspace()
    live = _live_frontmost_pid_name()
    if live is not None:
        pid, name = live
        # Resolve bundle id (and a better name) live by PID. We look the PID up
        # directly rather than scanning ws.runningApplications(), because that
        # cached list also misses freshly-launched apps without a run-loop pump.
        try:
            ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if ra is not None:
                return (pid, str(ra.localizedName() or name),
                        str(ra.bundleIdentifier() or ""))
        except Exception:
            pass
        return (pid, name, "")
    # Fallback: NSWorkspace (may be stale, but better than nothing).
    try:
        app = ws.frontmostApplication()
        if app is not None:
            return (int(app.processIdentifier()),
                    str(app.localizedName() or ""),
                    str(app.bundleIdentifier() or ""))
    except Exception:
        pass
    return None, "", ""


# ── Browser accessibility enabling (the keystone trick) ─────────────────────

def enable_web_accessibility(app_element: Any, bundle_id: str) -> bool:
    """
    Chromium/Safari keep web-content accessibility OFF by default and only turn
    it on when a client sets AXManualAccessibility (preferred) or
    AXEnhancedUserInterface on the app element — exactly how VoiceOver/Electron
    do it. We set it so page elements appear in the tree.

    Returns True if we attempted to enable (caller should re-read after a beat).
    """
    is_browser = bundle_id in _BROWSER_BUNDLES or bundle_id in _SAFARI_BUNDLES
    if not is_browser:
        return False

    attempted = False
    # Prefer AXManualAccessibility — avoids the window-manager side effects that
    # AXEnhancedUserInterface can cause.
    for attr in ("AXManualAccessibility", "AXEnhancedUserInterface"):
        try:
            AXUIElementSetAttributeValue(app_element, attr, True)
            attempted = True
        except Exception:
            continue
    return attempted


# ── Element classification ──────────────────────────────────────────────────

def _is_clickable(role: str, actions: list[str]) -> bool:
    """An element is clickable if it has AXPress, or is a known interactive role."""
    if "AXPress" in actions:
        return True
    return role in INTERACTIVE_ROLES


def _is_typeable(role: str, actions: list[str]) -> bool:
    return role in {
        "AXTextField", "AXTextArea", "AXSearchField",
        "AXSecureTextField", "AXComboBox",
    }


def _is_stateful(role: str, subrole: str) -> bool:
    """A binary state control whose on/off we should expose: a switch/toggle
    (AXCheckBox with subrole AXSwitch — the modern macOS toggle), a plain
    checkbox, or a radio button. Their AXValue is 1 (on) / 0 (off)."""
    return subrole == "AXSwitch" or role in {"AXCheckBox", "AXRadioButton"}


def _bool_state(value: Any) -> str:
    """Render a stateful control's AXValue as 'ON' / 'OFF', or '' if unknown.
    macOS toggles/checkboxes report AXValue as an integer 1 (on) / 0 (off)."""
    if value is None:
        return ""
    try:
        return "ON" if int(value) else "OFF"
    except (TypeError, ValueError):
        try:
            return "ON" if bool(value) else "OFF"
        except Exception:
            return ""


def _label_from_title_ui(element: Any) -> str:
    """
    Some controls have no title of their own but are titled by a SIBLING element
    referenced via AXTitleUIElement. This is exactly how macOS names System
    Settings toggles: the AXSwitch's own title/description are empty, but its
    AXTitleUIElement points at a label element holding e.g. "Bluetooth". Follow
    it and read that element's label.
    """
    te = _copy_attr(element, "AXTitleUIElement")
    if te is None:
        return ""
    for attr in (kAXTitleAttribute, kAXValueAttribute, kAXDescriptionAttribute):
        s = _str(_copy_attr(te, attr))
        if s:
            return s
    return ""


def _best_label(element: Any, role: str) -> str:
    """
    Pick the most human-meaningful label for an element, trying several
    attributes in priority order. This is what the model reads to choose.
    """
    for attr in (kAXTitleAttribute, kAXDescriptionAttribute):
        label = _str(_copy_attr(element, attr))
        if label:
            return label
    # Control with no title of its own but titled by a sibling (toggles, etc.).
    label = _label_from_title_ui(element)
    if label:
        return label
    # For text inputs, fall back to current value or placeholder.
    if role in {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}:
        val = _str(_copy_attr(element, kAXValueAttribute))
        if val:
            return f"({val})"  # parens signal "current content", not a label
        ph = _str(_copy_attr(element, kAXPlaceholderValueAttribute))
        if ph:
            return ph
    # Static text often IS the label (links rendered as text, etc.)
    if role == "AXStaticText":
        return _str(_copy_attr(element, kAXValueAttribute))
    return ""


# ── Recursive traversal ─────────────────────────────────────────────────────

def _walk(
    element: Any,
    out: list[dict],
    depth: int,
    deadline: float,
    seen_ids: set[int],
    in_list: bool = False,
) -> None:
    """
    Depth-first walk. Appends interactive elements to `out`. Stops on depth,
    element cap, or time budget.

    `in_list` is True once we're inside a sidebar/list container
    (LIST_CONTAINER_ROLES). Inside those, labelled NAVROW_ROLES (AXStaticText /
    AXCell / AXRow) are promoted to clickable even without AXPress, so navigation
    items like the System Settings sidebar become numbered and selectable.
    """
    if depth > MAX_DEPTH:
        return
    if len(out) >= MAX_ELEMENTS:
        return
    if time.monotonic() > deadline:
        return

    role = _str(_copy_attr(element, kAXRoleAttribute))
    if not role:
        return

    actions = _action_names(element)
    clickable = _is_clickable(role, actions)
    # Promote sidebar/list text rows that aren't natively clickable.
    navrow = (not clickable) and in_list and role in NAVROW_ROLES

    if clickable or navrow:
        pos = _get_point(element, kAXPositionAttribute)
        size = _get_size(element, kAXSizeAttribute)
        if pos and size and size[0] > 1 and size[1] > 1:
            label = _best_label(element, role)
            typeable = _is_typeable(role, actions)
            # Stateful controls (switches/checkboxes/radios): expose their on/off
            # state so the model + verifier can decide/verify without guessing.
            subrole = _str(_copy_attr(element, kAXSubroleAttribute))
            stateful = _is_stateful(role, subrole)
            state = _bool_state(_copy_attr(element, kAXValueAttribute)) if stateful else ""
            # AXEnabled: False means the control is present but greyed out — the
            # model should skip it. None (absent) is treated as enabled.
            en = _copy_attr(element, kAXEnabledAttribute)
            disabled = en is not None and not bool(en)
            # Include stateful controls even when label-less (a switch with no
            # name is still actionable); give them a sensible fallback name.
            if not label:
                label = "toggle" if stateful else ("input" if typeable else "")
            # Skip nameless generic buttons that are almost always noise, unless
            # they're a real input or a stateful control. Promoted nav rows are
            # only useful when they carry a label.
            if label or typeable or stateful:
                cx = int(pos[0] + size[0] / 2)
                cy = int(pos[1] + size[1] / 2)
                # Dedupe overlapping elements at (near) the same center.
                key = (cx // 6, cy // 6, role)
                kid = hash(key) & 0x7FFFFFFF
                if kid not in seen_ids:
                    seen_ids.add(kid)
                    out.append({
                        "role": role,
                        "subrole": subrole,
                        "name": label or "input",
                        "x": cx,
                        "y": cy,
                        "w": int(size[0]),
                        "h": int(size[1]),
                        "clickable": True,
                        "typeable": typeable,
                        "state": state,
                        "enabled": not disabled,
                        "focused": bool(_copy_attr(element, kAXFocusedAttribute)),
                    })

    # Descend into children. We descend through interactive elements too because
    # web areas nest clickable items inside other clickable groups. Once inside a
    # list/outline/table, keep `in_list` set so nested rows stay promotable.
    child_in_list = in_list or role in LIST_CONTAINER_ROLES
    children = _copy_attr(element, kAXChildrenAttribute)
    if children:
        for child in children:
            if len(out) >= MAX_ELEMENTS or time.monotonic() > deadline:
                break
            _walk(child, out, depth + 1, deadline, seen_ids, child_in_list)


# ── Public API ──────────────────────────────────────────────────────────────

def _find_modal(window: Any) -> Optional[Any]:
    """
    If a modal sheet/popover is open within `window`, return it (so we traverse
    ONLY it). Sheets attach as a direct child of the window; popovers too. We
    scan direct children (cheap) for a MODAL_ROLES element.
    """
    children = _copy_attr(window, kAXChildrenAttribute)
    if not children:
        return None
    for c in children:
        if _str(_copy_attr(c, kAXRoleAttribute)) in MODAL_ROLES:
            return c
    return None


def get_elements(retry_after_enable: bool = True) -> list[dict]:
    """
    Return a numbered list of interactive elements for the frontmost app.

    Each element: {id, role, name, x, y, w, h, clickable, typeable, focused}
    Coordinates (x, y) are the REAL screen center of the element, ready to click.

    The 'id' is a 1-based number assigned here — this is what the model picks.
    """
    pid, app_name, bundle_id = get_frontmost_app()
    if pid is None:
        return []

    app_element = AXUIElementCreateApplication(pid)
    if app_element is None:
        return []

    # Turn on web-content accessibility for browsers before reading.
    enabled = enable_web_accessibility(app_element, bundle_id)
    if enabled and retry_after_enable:
        # Give Chromium/Safari a moment to build the tree the first time.
        time.sleep(0.35)

    out: list[dict] = []
    seen: set[int] = set()
    deadline = time.monotonic() + TRAVERSAL_BUDGET_S

    # Prefer walking the focused/main window first (most relevant), then fall
    # back to all windows, then the app element itself.
    roots: list[Any] = []
    main_win = _copy_attr(app_element, kAXMainWindowAttribute)
    if main_win is not None:
        roots.append(main_win)
    windows = _copy_attr(app_element, kAXWindowsAttribute)
    if windows:
        for w in windows:
            if w is not main_win:
                roots.append(w)
    if not roots:
        roots.append(app_element)

    # If a modal sheet/popover is open on any window, it captures all input — walk
    # ONLY it, so we never surface elements occluded behind it (clicking those
    # would land on the modal overlay = a wrong-spot click).
    for w in roots:
        modal = _find_modal(w)
        if modal is not None:
            roots = [modal]
            break

    for root in roots:
        if len(out) >= MAX_ELEMENTS or time.monotonic() > deadline:
            break
        _walk(root, out, 0, deadline, seen)

    # Assign 1-based IDs after collection.
    for i, el in enumerate(out, start=1):
        el["id"] = i

    return out


# Maps an AX role to a compact, human-clear CONTROL KIND shown to the model. This
# is more informative than a bare "CLICK" — it lets the model tell a TOGGLE from a
# BUTTON from a POPUP, which is exactly the decision quality lever.
_KIND_MAP = {
    "AXButton": "BUTTON",
    "AXMenuButton": "BUTTON",
    "AXPopUpButton": "POPUP",
    "AXMenuItem": "MENUITEM",
    "AXMenuBarItem": "MENUITEM",
    "AXCheckBox": "CHECKBOX",
    "AXRadioButton": "RADIO",
    "AXLink": "LINK",
    "AXTabButton": "TAB",
    "AXDisclosureTriangle": "DISCLOSURE",
    "AXSlider": "SLIDER",
    "AXIncrementor": "STEPPER",
    "AXSegmentedControl": "SEGMENT",
    "AXComboBox": "COMBOBOX",
}


def _kind_label(e: dict) -> str:
    """Control kind for the prompt: TOGGLE wins (subrole AXSwitch), then TYPE,
    then the role map, then a generic CLICK."""
    if e.get("subrole") == "AXSwitch":
        return "TOGGLE"
    if e.get("typeable"):
        return "TYPE"
    return _KIND_MAP.get(e.get("role", ""), "CLICK")


def format_for_prompt(elements: list[dict]) -> str:
    """
    Render the numbered element list for the model prompt. The model reads this
    and replies with an element_id. Format is intentionally compact but carries
    the decision-critical signal: CONTROL KIND, label, and STATE for toggles.

      [3] TOGGLE "Bluetooth" (ON)
      [5] BUTTON "Turn Bluetooth Off"
      [7] TYPE "Search"
      [9] BUTTON "Submit" (disabled)
    """
    if not elements:
        return "(no accessible elements found — request a scroll, or a visual fallback)"
    lines = []
    for e in elements:
        line = f"  [{e['id']}] {_kind_label(e)} \"{e.get('name', '')}\""
        state = e.get("state", "")
        if state:
            line += f" ({state})"
        if e.get("enabled", True) is False:
            line += " (disabled)"
        if e.get("focused"):
            line += " *focused*"
        lines.append(line)
    return "\n".join(lines)


def find_by_id(elements: list[dict], eid: int) -> Optional[dict]:
    for e in elements:
        if e.get("id") == eid:
            return e
    return None


# ── CLI smoke test ──────────────────────────────────────────────────────────
# Run:  python ax_tree.py
# Switch to the app you want to inspect within ~2 seconds.

if __name__ == "__main__":
    import sys

    delay = 2.0
    if len(sys.argv) > 1:
        try:
            delay = float(sys.argv[1])
        except ValueError:
            pass

    print(f"Switch to the target app… reading in {delay:.0f}s")
    time.sleep(delay)

    pid, name, bundle = get_frontmost_app()
    print(f"\nFrontmost: {name}  (pid={pid}, bundle={bundle})")
    t0 = time.monotonic()
    els = get_elements()
    dt = (time.monotonic() - t0) * 1000
    print(f"Found {len(els)} interactive elements in {dt:.0f} ms:\n")
    print(format_for_prompt(els))
    print("\nRaw coords (first 10):")
    for e in els[:10]:
        print(f"  [{e['id']}] ({e['x']},{e['y']}) {e['w']}x{e['h']} {e['role']} :: {e['name']}")
