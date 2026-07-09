"""
test_reflexion.py — unit tests for the Reflexion mechanisms (verbal self-
improvement): reflection-memory injection, the disabled-action set, and the
goal-level loop guard.

Pure logic — NO Mac, NO mouse, NO live Telegram, NO network (the model is mocked
for the orchestrator.reflect/diagnose calls). Covers the agent_core pure helpers
and the orchestrator reflection calls' contract.

The Calculator "clear / re-enter" doubt loop is the motivating case: distinct
actions that repeat without verified progress, where the world changes each turn
so the old same-action stuck-detector never fires.

Run:
    .venv/bin/python agents/test_reflexion.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import agent_core as ac
import orchestrator as orch


def _check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


# ── _is_committing ──────────────────────────────────────────────────────────
print("is_committing")
_check(ac._is_committing({"action": "type", "text": "47"}) is True, "type is committing")
_check(ac._is_committing({"action": "click_element", "element_id": 3}) is True, "click is committing")
_check(ac._is_committing({"action": "open_app", "app": "Calculator"}) is True, "open_app is committing")
_check(ac._is_committing({"action": "press_key", "key": "return"}) is True, "press_key is committing")
_check(ac._is_committing({"action": "scroll", "direction": "down"}) is False, "scroll NOT committing")
_check(ac._is_committing({"action": "wait", "duration": 2}) is False, "wait NOT committing")


# ── _reflexion_update (disabled-set accounting) ─────────────────────────────
print("reflexion_update (disabled set + repeat counting)")
sigs, reps = ac._reflexion_update("type:47", set(), 0)
_check(reps == 0 and "type:47" in sigs, "first distinct action -> recorded, 0 repeats")
sigs, reps = ac._reflexion_update("type:89", sigs, reps)
_check(reps == 0 and len(sigs) == 2, "second distinct action -> still 0 repeats")
# The Calculator doubt loop: clear, then RE-type the same keys.
sigs, reps = ac._reflexion_update("press_key:escape", sigs, reps)   # clear (new)
_check(reps == 0, "clear is new -> 0 repeats")
sigs, reps = ac._reflexion_update("type:47", sigs, reps)            # re-type 47 (REPEAT)
_check(reps == 1, "re-typing 47 -> 1 repeat (doubt signal)")
sigs, reps = ac._reflexion_update("type:47", sigs, reps)            # again
_check(reps == 2, "another repeat -> 2")
sigs, reps = ac._reflexion_update("type:89", sigs, reps)            # re-type 89 (REPEAT)
_check(reps == 3, "re-typing 89 -> 3 -> trips the guard at limit 3")
# Doesn't mutate the caller's set
owned = set()
ac._reflexion_update("type:1", owned, 0)
_check(owned == set(), "input set not mutated")


# ── _goal_loop_should_reflect / _goal_loop_should_trip ──────────────────────
print("goal-loop reflect + trip rules")
_check(ac._goal_loop_should_reflect(0, False) is False, "no repeats -> don't reflect yet")
_check(ac._goal_loop_should_reflect(1, False) is True, "1 repeat -> reflect (default after=1)")
_check(ac._goal_loop_should_reflect(3, True) is False, "already reflected -> don't again")
_check(ac._goal_loop_should_reflect(1, False, after=2) is False, "custom after=2 respected")
_check(ac._goal_loop_should_trip(2) is False, "2 repeats -> not tripped (limit 3)")
_check(ac._goal_loop_should_trip(3) is True, "3 repeats -> TRIPPED (limit 3)")
_check(ac._goal_loop_should_trip(2, limit=2) is True, "custom limit respected")


# ── _reflection_block (prompt injection) ────────────────────────────────────
print("reflection_block (memory + disabled-set injection)")
_check(ac._reflection_block([], [], 0) == "", "nothing -> empty injection")
# Lessons only (no repetition yet): inject lessons, NOT the disabled set.
b = ac._reflection_block(["read the display, don't re-enter"], [], 0)
_check("LESSONS FROM YOUR PAST MISTAKES" in b and "read the display" in b, "lessons injected")
_check("REPEATING" not in b, "disabled set hidden while goal_repeats==0 (no false 'failed')")
# Repeating: disabled set now surfaced, with the tried actions.
b = ac._reflection_block([], ["type 47", "type 89"], 2)
_check("REPEATING" in b and "type 47" in b and "type 89" in b, "disabled set shown once repeating")
# Sliding window: only the last `window` lessons are kept.
many = [f"lesson {i}" for i in range(5)]
b = ac._reflection_block(many, [], 0, window=4)
_check("lesson 0" not in b and "lesson 4" in b, "reflection window keeps last N lessons")


# ── orchestrator.reflect / diagnose (model mocked) ──────────────────────────
print("orchestrator reflect/diagnose contract")


class _FakeModel:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def decide(self, messages, **kw):
        self.calls += 1
        return self.payload


m = _FakeModel({"lesson": "You already entered 47×89 — the display shows 4183; stop and read it."})
lesson = orch.reflect(m, "compute 47 times 89", "result visible",
                      ["type 47", "type 89"], "App: Calculator", "[1] DISPLAY '4183'", [])
_check(lesson.startswith("You already entered"), "reflect() pulls the lesson from model output")
_check(m.calls == 1, "reflect() makes exactly one model call")

# reflect() swallows a None (rate-limited/failed) model reply -> ""
_check(orch.reflect(_FakeModel(None), "t", "g", [], "c", "e", []) == "", "reflect None -> ''")

# diagnose() normalises an unknown verdict to "stuck"
d = orch.diagnose(_FakeModel({"verdict": "DONE", "lesson": "x", "reason": "r"}),
                  "t", "g", [], "c", "e", [])
_check(d["verdict"] == "stuck", "diagnose normalises unknown verdict -> stuck")
d = orch.diagnose(_FakeModel({"verdict": "done_missed", "lesson": "display shows 4183"}),
                  "t", "g", [], "c", "e", [])
_check(d["verdict"] == "done_missed" and d["lesson"] == "display shows 4183",
       "diagnose passes through done_missed + lesson")
# diagnose() on a failed model call -> {} (caller treats as stuck -> asks user)
_check(orch.diagnose(_FakeModel(None), "t", "g", [], "c", "e", []) == {},
       "diagnose None -> {} (graceful)")

print("\nAll reflexion tests passed.")
