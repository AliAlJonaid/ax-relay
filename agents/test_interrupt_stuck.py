"""
test_interrupt_stuck.py — unit tests for the live-interrupt mailbox + stuck
detection (LEFT-list item 1).

Pure logic only — NO Mac, NO mouse, NO live Telegram. Covers:
  • agent_core._action_sig / _action_human / _stuck_eval (the stuck rule)
  • telegram_bridge.Session mailbox (add/drain) + answer routing
    (deliver_answer returns True when a question is pending, else the text is
    treated as live guidance instead).

Run:
    .venv/bin/python agents/test_interrupt_stuck.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import agent_core
import telegram_bridge as tb


def _check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


# ── action helpers ──────────────────────────────────────────────────────────
print("action helpers")
_check(agent_core._action_sig({"action": "click_element", "element_id": 7})
       == "click_element:7", "action_sig(click_element)")
_check(agent_core._action_sig({"action": "type", "text": "hi"})
       == "type:hi", "action_sig(type)")
_check(agent_core._action_sig({"action": "scroll", "direction": "down"})
       == "scroll:down", "action_sig(scroll)")
_check(agent_core._action_human({"action": "click_element", "element_id": 3})
       == "click element #3", "action_human(click)")
_check(agent_core._action_human({"action": "scroll", "direction": "down"})
       == "scroll down", "action_human(scroll)")
_check("type" in agent_core._action_human({"action": "type", "text": "hello world"})
       and "hello worl" in agent_core._action_human({"action": "type", "text": "hello world"}),
       "action_human(type) truncates")

# ── _stuck_eval rule ────────────────────────────────────────────────────────
print("stuck rule")
L = 3
s, ask = agent_core._stuck_eval("click_element", "click:1", "W", None, None, 1, L)
_check(s == 1 and not ask, "first action: streak=1, no ask")
s, ask = agent_core._stuck_eval("click_element", "click:1", "W", "click:1", "W", 1, L)
_check(s == 2 and not ask, "repeat #1: streak=2, no ask")
s, ask = agent_core._stuck_eval("click_element", "click:1", "W", "click:1", "W", 2, L)
_check(s == 3 and ask, "repeat #2: streak=3 -> ASK (3x no-change)")
s, ask = agent_core._stuck_eval("click_element", "click:1", "W2", "click:1", "W", 2, L)
_check(s == 1 and not ask, "world changed -> reset")
s, ask = agent_core._stuck_eval("scroll", "scroll:down", "W", "click:1", "W", 2, L)
_check(s == 1 and not ask, "different action -> reset")
s, ask = agent_core._stuck_eval("done", "done:", "W", "click:1", "W", 2, L)
_check(s == 1 and not ask, "terminal 'done' ignored")
s, ask = agent_core._stuck_eval("ask_user", "ask_user:", "W", "ask_user:", "W", 2, L)
_check(s == 1 and not ask, "introspective 'ask_user' ignored")

# ── Session interrupt mailbox ───────────────────────────────────────────────
print("Session mailbox")
sess = tb.Session(app=None, loop=None, chat_id=12345)
_check(sess.drain_interrupts() is None, "empty mailbox -> None")
_check(not sess.has_pending_question(), "no pending question initially")

# While no question is pending, plain text is queued as mid-task guidance.
sess.add_interrupt("click element 5")
sess.add_interrupt("then scroll down")
out = sess.drain_interrupts()
_check(out == "click element 5\nthen scroll down", "two interrupts combined & cleared")
_check(sess.drain_interrupts() is None, "mailbox empty after drain")

# When the worker thread is blocked on a question, the next reply ANSWERS it
# (deliver_answer True) instead of being queued as guidance.
sess._pending_kind = "ask"
_check(sess.has_pending_question(), "pending question detected")
ok = sess.deliver_answer("yes do it")
_check(ok, "deliver_answer True when a question is pending")
_check(sess._answer_text == "yes do it", "answer text stored")

# No question pending -> a reply is NOT consumed (caller queues it as guidance).
sess._pending_kind = None
ok = sess.deliver_answer("orphan reply")
_check(not ok, "deliver_answer False when no question pending -> becomes interrupt")

print("\nAll interrupt/stuck tests passed.")
