"""
test_appname_verify.py — unit tests for the open_app name resolver and the
verifier's stale-state (baseline) guard.

Pure logic, no Mac, no network (the installed-app list is stubbed; the model is
mocked). Covers:
  • executor.resolve_app_name — turns guessed app names ("WhatsApp Desktop") into
    the real installed name ("WhatsApp").
  • orchestrator.verify — passes the task-START baseline to the model so the
    stale-state guard can reject pre-existing results.

Run:  .venv/bin/python agents/test_appname_verify.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import executor as ex
import orchestrator as orch


def _check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


# ── resolve_app_name ────────────────────────────────────────────────────────
print("resolve_app_name (fixes wrong-app guesses)")
ex._list_app_names = lambda: {
    "whatsapp": "WhatsApp", "safari": "Safari",
    "system settings": "System Settings", "calculator": "Calculator",
}
_check(ex.resolve_app_name("WhatsApp") == "WhatsApp", "exact name")
_check(ex.resolve_app_name("whatsapp") == "WhatsApp", "case-insensitive")
_check(ex.resolve_app_name("WhatsApp Desktop") == "WhatsApp", "'WhatsApp Desktop' → 'WhatsApp'")
_check(ex.resolve_app_name("WhatsApp Messenger") == "WhatsApp", "'WhatsApp Messenger' → 'WhatsApp'")
_check(ex.resolve_app_name("safari") == "Safari", "other app resolves")
_check(ex.resolve_app_name("Definitely Not An App xyz123") is None, "unknown → None")


# ── verify baseline (stale-state guard) ─────────────────────────────────────
print("verify baseline (stale-state guard)")
captured = {}


class _M:
    def decide(self, messages, **kw):
        captured["user"] = messages[-1]["content"]
        return {"achieved": False, "reason": "not yet"}


m = _M()
orch.verify(m, "the chat with أكرم is open",
            "App: WhatsApp\nWindow: أكرم",
            "[1] FIELD ''  [2] BUTTON 'Send'",
            baseline_context="App: WhatsApp\nWindow: أكرم",
            baseline_elements="[1] FIELD ''")
_check("STATE AT TASK START" in captured["user"], "verify passes the START baseline to the model")
_check("BEFORE the agent acted" in captured["user"], "baseline is labelled as task-start")
_check("Send" in captured["user"] or "send" in captured["user"], "current elements included")

# verify still returns a bool from the model's achieved flag
class _M2:
    def decide(self, messages, **kw):
        return {"achieved": True, "reason": "new message sent"}
_check(orch.verify(_M2(), "x", "c", "e", "bc", "be") is True, "verify True when model says achieved")
class _M3:
    def decide(self, messages, **kw):
        return None
_check(orch.verify(_M3(), "x", "c", "e", "bc", "be") is False, "verify False on model failure (None)")

# Section-guard still short-circuits without a model call (no decide invoked)
class _Boom:
    def decide(self, messages, **kw):
        raise AssertionError("model should not be called when the section is unsupported")
_check(orch.verify(_Boom(), "System Settings is open on the Sound pane",
                   "App: System Settings", "[1] BUTTON 'Search'",
                   "App: System Settings", "[1] BUTTON 'Search'") is False,
       "section guard still short-circuits (no model call)")

print("\nAll app-name / verify-baseline tests passed.")


# ── send-goal gate ("send" is an ACTION, not a STATE) ───────────────────────
print("send-goal gate (send is an action, not visible text)")
_check(orch.is_send_goal('Message "مرحبا" appears as the latest sent message') is True,
       "is_send_goal: 'sent' sub-goal")
_check(orch.is_send_goal("send the message to أكرم") is True, "is_send_goal: 'send'")
_check(orch.is_send_goal("أرسل الرسالة") is True, "is_send_goal: Arabic أرسل")
_check(orch.is_send_goal("turn Bluetooth on") is False, "is_send_goal: state goal is NOT send")
_check(orch.is_send_goal("accept the consent prompt") is False,
       "is_send_goal: 'consent' is NOT a false hit (word boundary)")


class _Boom2:
    def decide(self, messages, **kw):
        raise AssertionError("model must NOT be called when the send gate blocks")


# A send goal with no send-action this run → False WITHOUT a model call, even if the
# target text is already on screen (the exact false-done bug).
_check(orch.verify(_Boom2(), 'send "مرحبا"',
                   "App: WhatsApp", "[1] TEXT 'مرحبا'", "App: WhatsApp", "[1] TEXT 'مرحبا'",
                   run_sent=False, run_typed=False) is False,
       "send goal NOT met when agent didn't send this run (pre-existing text ignored)")
# Once the agent typed + sent this run, the gate passes and the model decides content.
class _Ok:
    def decide(self, messages, **kw):
        return {"achieved": True}
_check(orch.verify(_Ok(), 'send "مرحبا"', "App: WhatsApp", "[1] 'مرحبا'",
                   "App: WhatsApp", "[]", run_sent=True, run_typed=True) is True,
       "send goal allowed through to the model once typed+sent this run")

print("\nAll app-name / verify-baseline tests passed (incl. send gate).")
