"""
test_executor_gate.py — unit tests for the destructive-action safety gate in
executor.py.

The gate is safety/security-critical and was previously UNTESTED. This file
covers:
  • Executor._is_destructive — every keyword, model-provided fields, selected
    AX-element metadata, case-insensitivity, empty/missing fields, and benign
    text → False.
  • The gate inside Executor.execute — all four branches (confirm_cb False →
    cancelled; confirm_cb True → proceeds; confirm_cb None → needs_confirmation;
    destructive-but-non-gated action type → not gated) and the non-destructive
    path.

NO Mac, NO network, NO real mouse/clipboard. Every OS-level helper that
execute() dispatches to is patched to a no-op at the executor module before
any Executor is constructed, so a missed branch can never move the real mouse
(this Mac auto-aborts if the mouse hits a corner).

Run from the project root:
      .venv/bin/python agents/test_executor_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import executor as E


def _check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


# Defensive: neutralize EVERY OS-level helper execute() can reach. Done once,
# before any Executor is built, so no branch can ever move the real mouse or
# touch the clipboard. These are module attributes on E, so assignments here
# patch what execute() actually resolves at call time.
E._move_and_settle = lambda *a, **k: None
E._type_via_clipboard = lambda *a, **k: None
E._press_key_safe = lambda *a, **k: None
# pyautogui is imported into E's namespace; patch it there too (click / scroll
# / doubleClick are called directly via pyautogui in the click/scroll branches).
E.pyautogui.click = lambda *a, **k: None
E.pyautogui.doubleClick = lambda *a, **k: None
E.pyautogui.scroll = lambda *a, **k: None
# Guard time.sleep so the test stays fast (real sleeps would slow it but not
# move the mouse). time is imported into E's namespace.
E.time.sleep = lambda *a, **k: None

# A reusable element list for the click path.
_ELEMENTS = [
    {"id": 3, "x": 100, "y": 200, "name": "Delete", "role": "AXButton"},
    {"id": 5, "x": 10, "y": 20, "name": "Send", "role": "AXButton"},
]

# A benign (non-destructive) base action used in several checks.
_BENIGN_CLICK = {"action": "click_element", "element_id": 5,
                 "thought": "clicking the Send button"}


# ── _is_destructive: keyword coverage ───────────────────────────────────────
print("_is_destructive — every keyword matches")
_all_kw_pass = True
for kw in E.DESTRUCTIVE_KEYWORDS:
    action = {"action": "click_element", "element_id": 3,
              "thought": f"I will {kw} the item"}
    if not E.Executor._is_destructive(action):
        _all_kw_pass = False
        print(f"    keyword did NOT match: {kw!r}")
_check(_all_kw_pass, "all DESTRUCTIVE_KEYWORDS match when present in 'thought'")


# ── _is_destructive: each of the four scanned fields is checked independently ─
print("_is_destructive — each scanned field is checked independently")
for field in ("thought", "text", "question", "summary"):
    action = {"action": "click_element", "element_id": 3,
              field: "delete this folder"}
    _check(E.Executor._is_destructive(action),
           f"keyword found in '{field}' field alone")


# ── _is_destructive: case-insensitivity & substring/partial match ───────────
print("_is_destructive — case-insensitivity and substring matching")
_check(E.Executor._is_destructive(
    {"thought": "I will DELETE everything"}), "uppercase keyword matches")
_check(E.Executor._is_destructive(
    {"thought": "Let me ReMoVe the file"}), "mixed-case keyword matches")
_check(E.Executor._is_destructive(
    {"text": "please checkout now"}), "lowercase keyword in 'text' matches")
# substring / partial: 'deleted' contains 'delete'; 'remover' contains 'remove'
_check(E.Executor._is_destructive(
    {"thought": "the file was deleted"}), "substring 'deleted' contains 'delete'")
_check(E.Executor._is_destructive(
    {"question": "use the remover tool"}), "substring 'remover' contains 'remove'")
_check(E.Executor._is_destructive(
    {"summary": "empty trash bin"}), "'empty trash bin' contains 'empty trash'")


# ── _is_destructive: selected AX metadata is part of the decision ───────────
print("_is_destructive — selected AX metadata is scanned")
_check(E.Executor._is_destructive(
    {"thought": "click the button"}, _ELEMENTS[0]),
    "neutral model wording + element name 'Delete' -> destructive")
_check(E.Executor._is_destructive(
    {"thought": "click the button"}, _ELEMENTS[1]) is False,
    "neutral model wording + benign element name -> False")


# ── _is_destructive: benign text and empty/missing fields → False ───────────
print("_is_destructive — benign and empty/missing inputs → False")
_check(E.Executor._is_destructive({"thought": "open Safari"}) is False,
       "benign thought → False")
_check(E.Executor._is_destructive({"thought": "scroll down a bit"}) is False,
       "no keyword present → False")
_check(E.Executor._is_destructive({}) is False,
       "empty dict (all fields missing) → False")
_check(E.Executor._is_destructive(
    {"thought": "", "text": "", "question": "", "summary": ""}) is False,
       "all fields present but empty strings → False")
# Field containing a keyword as a SUBSTRING of a larger unrelated word must
# still count (matches the substring contract). 'pay' inside 'payment'.
_check(E.Executor._is_destructive({"thought": "open the payment page"}) is True,
       "'pay' substring inside 'payment' still flags (substring contract)")


# ── Gate branch: confirm_cb returns False → cancelled ───────────────────────
print("gate — confirm_cb returns False → cancelled, success False")
ex_deny = E.Executor(confirm_cb=lambda q: False)
r = ex_deny.execute(
    {"action": "click_element", "element_id": 3, "thought": "delete the file"},
    _ELEMENTS)
_check(r.ok is False, "confirm_cb=False → result.ok False")
_check("Cancelled by user" in r.message,
       "confirm_cb=False → cancellation message")
_check(r.needs_confirmation is False,
       "confirm_cb=False → needs_confirmation False")


# ── Gate branch: confirm_cb returns True → gate passes, action proceeds ─────
print("gate — confirm_cb returns True → gate passes, executes (no-op patches)")
proceed_calls = {"n": 0}


def _yes(q):
    proceed_calls["n"] += 1
    return True


ex_allow = E.Executor(confirm_cb=_yes)
r = ex_allow.execute(
    {"action": "click_element", "element_id": 3, "thought": "delete the file"},
    _ELEMENTS)
_check(r.ok is True, "confirm_cb=True → result.ok True (gate passed)")
_check("Clicked" in r.message, "confirm_cb=True → click action ran")
_check(r.clicked_xy == (100, 200),
       "confirm_cb=True → real coordinate resolved from element")
_check(proceed_calls["n"] == 1, "confirm_cb invoked exactly once")


# ── Gate branch: confirm_cb is None → needs_confirmation, not executed ──────
print("gate — confirm_cb is None → needs_confirmation, not executed")
ex_none = E.Executor(confirm_cb=None)
r = ex_none.execute(
    {"action": "click_element", "element_id": 3, "thought": "delete the file"},
    _ELEMENTS)
_check(r.ok is False, "confirm_cb None → result.ok False")
_check(r.needs_confirmation is True,
       "confirm_cb None → needs_confirmation True")
_check("Awaiting confirmation" in r.message,
       "confirm_cb None → awaiting-confirmation message")
_check(r.question is not None and "destructive" in r.question,
       "confirm_cb None → question populated and mentions destructive")
_check("delete the file" in r.question,
       "confirm_cb None → question echoes the destructive thought")


# ── Gate branch: neutral thought + destructive target label is still gated ──
print("gate — selected element metadata cannot be hidden by neutral wording")
r = ex_none.execute(
    {"action": "click_element", "element_id": 3,
     "thought": "click the button"}, _ELEMENTS)
_check(r.ok is False and r.needs_confirmation is True,
       "neutral thought targeting 'Delete' -> needs confirmation")
_check(r.question is not None and "Delete" in r.question,
       "confirmation question names the destructive target")


# ── Gate branch: destructive text but action type NOT gated → not gated ─────
print("gate — destructive text but non-gated action type → gate skipped")
for act, extra in (("scroll", {"direction": "down", "amount": 3}),
                   ("wait", {"duration": 0}),
                   ("open_app", {"app": "Trash"})):
    action = {"action": act, "thought": "delete everything", **extra}
    r = ex_none.execute(action, _ELEMENTS)
    # None of these reach the destructive gate; they should NOT set
    # needs_confirmation and should report success (helpers patched to no-ops).
    _check(r.needs_confirmation is False,
           f"action '{act}' with destructive text → not flagged (no confirmation)")
    _check(r.ok is True,
           f"action '{act}' with destructive text → proceeds (success True)")


# Also exercise the press_key and type gated types with destructive text but a
# None confirm_cb: these ARE gated, so they must surface needs_confirmation.
print("gate — gated action types (press_key, type) surface confirmation")
r = ex_none.execute(
    {"action": "press_key", "key": "return", "thought": "confirm purchase"},
    _ELEMENTS)
_check(r.ok is False and r.needs_confirmation is True,
       "press_key with destructive text → gated (needs confirmation)")
r = ex_none.execute(
    {"action": "type", "text": "delete", "thought": "delete it"},
    _ELEMENTS)
_check(r.ok is False and r.needs_confirmation is True,
       "type with destructive text → gated (needs confirmation)")


# ── Non-destructive action → not gated, succeeds ────────────────────────────
print("gate — non-destructive action → not gated")
r = ex_none.execute(_BENIGN_CLICK, _ELEMENTS)
_check(r.ok is True, "benign click → success True")
_check(r.needs_confirmation is False,
       "benign click → needs_confirmation False")
_check(r.clicked_xy == (10, 20), "benign click → correct coordinate")


# ── double_click_element is in the gated set too ────────────────────────────
print("gate — double_click_element is gated and gated-set behaves as documented")
r = ex_none.execute(
    {"action": "double_click_element", "element_id": 3,
     "thought": "erase this"}, _ELEMENTS)
_check(r.ok is False and r.needs_confirmation is True,
       "double_click_element with destructive text → gated")
# And once confirmed, the double-click path resolves to the real coordinate.
r = ex_allow.execute(
    {"action": "double_click_element", "element_id": 3,
     "thought": "erase this"}, _ELEMENTS)
_check(r.ok is True and r.clicked_xy == (100, 200),
       "double_click_element after confirm → success + real coordinate")


print("\nAll executor-gate tests passed.")
