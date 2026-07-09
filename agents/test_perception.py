"""
test_perception.py — unit tests for the enriched perception layer (no Mac needed).

Covers the pure helpers added to ax_tree.py:
  • _bool_state        — AXValue 1/0 → ON/OFF
  • _is_stateful       — switch/checkbox/radio detection (incl. subrole AXSwitch)
  • _kind_label        — TOGGLE vs CHECKBOX vs BUTTON vs TYPE vs CLICK
  • format_for_prompt  — renders state, (disabled), *focused*

Run:
    .venv/bin/python agents/test_perception.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import ax_tree as ax


def _check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


# ── _bool_state ─────────────────────────────────────────────────────────────
print("bool_state")
_check(ax._bool_state(None) == "", "None -> unknown")
_check(ax._bool_state(0) == "OFF", "0 -> OFF")
_check(ax._bool_state(1) == "ON", "1 -> ON")
_check(ax._bool_state(2) == "ON", "2 (truthy) -> ON")
_check(ax._bool_state(True) == "ON", "True -> ON")
_check(ax._bool_state(False) == "OFF", "False -> OFF")

# ── _is_stateful ────────────────────────────────────────────────────────────
print("is_stateful")
_check(ax._is_stateful("AXCheckBox", "AXSwitch"), "AXCheckBox+AXSwitch (the toggle) -> stateful")
_check(ax._is_stateful("AXCheckBox", ""), "AXCheckBox alone -> stateful")
_check(ax._is_stateful("AXRadioButton", ""), "AXRadioButton -> stateful")
_check(ax._is_stateful("AXButton", "AXSwitch"), "AXButton+AXSwitch -> stateful (subrole wins)")
_check(not ax._is_stateful("AXButton", ""), "plain AXButton -> not stateful")
_check(not ax._is_stateful("AXTextField", ""), "AXTextField -> not stateful")

# ── _kind_label ─────────────────────────────────────────────────────────────
print("kind_label")
_check(ax._kind_label({"role": "AXCheckBox", "subrole": "AXSwitch"}) == "TOGGLE",
       "toggle -> TOGGLE (subrole wins over AXCheckBox)")
_check(ax._kind_label({"role": "AXCheckBox", "subrole": ""}) == "CHECKBOX",
       "plain checkbox -> CHECKBOX")
_check(ax._kind_label({"role": "AXRadioButton", "subrole": ""}) == "RADIO", "radio -> RADIO")
_check(ax._kind_label({"role": "AXButton", "subrole": "", "typeable": False}) == "BUTTON",
       "button -> BUTTON")
_check(ax._kind_label({"role": "AXTextField", "typeable": True}) == "TYPE", "textfield -> TYPE")
_check(ax._kind_label({"role": "AXPopUpButton"}) == "POPUP", "popup -> POPUP")
_check(ax._kind_label({"role": "AXLink"}) == "LINK", "link -> LINK")
_check(ax._kind_label({"role": "AXStaticText"}) == "CLICK", "static-text nav -> CLICK")

# ── format_for_prompt ───────────────────────────────────────────────────────
print("format_for_prompt")
els = [
    {"id": 1, "role": "AXButton", "subrole": "", "name": "Search",
     "typeable": False, "state": "", "enabled": True, "focused": False},
    {"id": 2, "role": "AXCheckBox", "subrole": "AXSwitch", "name": "Bluetooth",
     "typeable": False, "state": "ON", "enabled": True, "focused": False},
    {"id": 3, "role": "AXButton", "subrole": "", "name": "Turn Bluetooth Off",
     "typeable": False, "state": "", "enabled": False, "focused": False},
    {"id": 4, "role": "AXTextField", "subrole": "", "name": "Address",
     "typeable": True, "state": "", "enabled": True, "focused": True},
]
out = ax.format_for_prompt(els).splitlines()
_check(out[0] == '  [1] BUTTON "Search"', "button renders as BUTTON")
_check(out[1] == '  [2] TOGGLE "Bluetooth" (ON)', "toggle renders with (ON)  <-- the fix")
_check(out[2] == '  [3] BUTTON "Turn Bluetooth Off" (disabled)', "disabled marked")
_check(out[3] == '  [4] TYPE "Address" *focused*', "type + focused")
_check(ax.format_for_prompt([]).startswith("(no accessible"), "empty list message")

print("\nAll perception tests passed.")
