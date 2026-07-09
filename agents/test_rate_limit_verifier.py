"""
test_rate_limit_verifier.py — unit tests for the multi-provider failover chain,
the rate-limit give-up policy, and the verifier section-guard.

Pure logic — NO Mac, NO mouse, NO live Telegram, NO network (OpenAI clients are
mocked). Covers:
  • model_client._parse_chain / _find_model_id / _should_failover / _retry_after_seconds
  • model_client.ModelClient.decide(return_reason=True):
        - 4-tuple (action, reason, retry_after_s, served_display)
        - chain failover: primary 429 → advances to provider 2 → succeeds (and the
          switched-to provider is reported)
        - whole chain 429 → returns rate_limit (triggers agent_core's give-up)
        - dead link (auth/expired key) is skipped like any unavailable provider
        - parse error does NOT fail over (request-level), stays on the provider
        - single-call 429 break; backward-compat bare-dict path
  • orchestrator._section_goal_unsupported  ("BT pane from app name alone" guard)
  • agent_core._rate_limit_should_abort / _watchdog_expired

Run:
    .venv/bin/python agents/test_rate_limit_verifier.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))

import model_client as mc
import orchestrator as orch


def _check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


# ── fakes ───────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, h):
        self.headers = h


class _Err(Exception):
    """Mimics openai.APIStatusError: str() carries the message, .response.headers
    carries Retry-After (read by _retry_after_seconds)."""
    def __init__(self, msg, h=None):
        super().__init__(msg)
        self.response = _Resp(h or {})


def _ok_ep(name, model="m"):
    """Endpoint whose create() returns a valid 'done' action."""
    def create(**kw):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"action":"done","summary":"ok"}'))])
    return SimpleNamespace(provider=name.lower(), model=model, label=name,
                           display=f"{name} ({model})",
                           client=SimpleNamespace(chat=SimpleNamespace(
                               completions=SimpleNamespace(create=create))))


def _raise_ep(name, exc, model="m"):
    """Endpoint whose create() raises `exc`."""
    def create(**kw):
        raise exc
    return SimpleNamespace(provider=name.lower(), model=model, label=name,
                           display=f"{name} ({model})",
                           client=SimpleNamespace(chat=SimpleNamespace(
                               completions=SimpleNamespace(create=create))))


def _count_ep(name, counter, model="m"):
    """Endpoint that counts calls and succeeds."""
    def create(**kw):
        counter[name] = counter.get(name, 0) + 1
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"action":"done","summary":"ok"}'))])
    return SimpleNamespace(provider=name.lower(), model=model, label=name,
                           display=f"{name} ({model})",
                           client=SimpleNamespace(chat=SimpleNamespace(
                               completions=SimpleNamespace(create=create))))


def _mkclient(endpoints):
    m = mc.ModelClient.__new__(mc.ModelClient)
    m.endpoints = endpoints
    m.temperature = 0.0
    m.max_tokens = 16
    m.timeout = 60
    m.min_call_interval = 0.0
    m._last_call_at = 0.0
    m._preferred_idx = 0
    p = endpoints[0]
    m.client, m.model, m.label = p.client, p.model, p.label
    m.chain_display = " → ".join(e.label for e in endpoints)
    return m


SYS = [{"role": "system", "content": "x"}]


# ── _parse_chain ────────────────────────────────────────────────────────────
print("parse_chain")
_check(mc._parse_chain("groq:openai/gpt-oss-120b,cerebras:gpt-oss-120b")
       == [("groq", "openai/gpt-oss-120b"), ("cerebras", "gpt-oss-120b")],
       "provider:model pairs parsed")
_check(mc._parse_chain("groq, google: gemini-3-flash ")
       == [("groq", ""), ("google", "gemini-3-flash")],
       "bare provider → empty model; whitespace trimmed")


# ── _find_model_id ──────────────────────────────────────────────────────────
print("find_model_id")
cat = ["openai/gpt-oss-120b", "gemini-3.0-flash-001", "gemini-3.0-pro",
       "gemini-2.0-flash", "llama-3.3-70b"]
_check(mc._find_model_id("openai/gpt-oss-120b", cat) == "openai/gpt-oss-120b",
       "exact match")
_check(mc._find_model_id("gpt-oss-120b", cat) == "openai/gpt-oss-120b",
       "normalized containment (org prefix)")
_check(mc._find_model_id("gemini-3-flash", cat) == "gemini-3.0-flash-001",
       "token overlap picks canonical gemini-3-flash id")
_check(mc._find_model_id("does-not-exist-xyz", cat) is None, "no match -> None")


# ── _should_failover ────────────────────────────────────────────────────────
print("should_failover")
fo, reason, ra = mc._should_failover("Error code: 429 - Rate limit reached",
                                     _Err("x", {"retry-after": "15"}))
_check(fo and reason == "rate_limit" and ra == 15.0, "429 -> failover rate_limit")
fo, reason, _ = mc._should_failover("Error code: 400 - API key expired. Please renew.",
                                    _Err("x"))
_check(fo and reason == "auth", "expired key -> failover auth")
fo, reason, _ = mc._should_failover("Request timed out", _Err("x"))
_check(fo and reason == "timeout", "timeout -> failover")
fo, reason, _ = mc._should_failover("Connection error.", _Err("x"))
_check(fo and reason == "connection", "connection -> failover")
fo, reason, _ = mc._should_failover("Error code: 503 - Service Unavailable", _Err("x"))
_check(fo and reason == "server_error", "5xx -> failover")
fo, _, _ = mc._should_failover("Error code: 413 - Request too large", _Err("x"))
_check(fo is False, "413 payload error -> NO failover")
fo, _, _ = mc._should_failover("Error code: 400 - bad request shape", _Err("x"))
_check(fo is False, "generic 400 -> NO failover")


# ── retry-after parsing ─────────────────────────────────────────────────────
print("retry-after parsing")
_check(mc._retry_after_seconds(_Err("429", {"retry-after": "30"})) == 30.0, "numeric honored")
_check(mc._retry_after_seconds(_Err("429", {"Retry-After": "12"})) == 12.0, "title-case honored")
_check(mc._retry_after_seconds(_Err("429", {})) == 20.0, "no header -> default 20")
_check(mc._retry_after_seconds(_Err("429", {"retry-after": "9999"})) == 60.0, "capped at 60")
_check(mc._retry_after_seconds(_Err("429", {"retry-after": "1"})) == 3.0, "floored at 3")


# ── decide: chain failover ──────────────────────────────────────────────────
print("decide chain failover")
# primary 429 -> advances to provider 2 -> succeeds; provider 3 never called.
counts = {}
m = _mkclient([_raise_ep("Groq", _Err("Error code: 429 - Rate limit", {"retry-after": "10"})),
               _count_ep("Cerebras", counts),
               _count_ep("Google", counts)])
act, reason, ra, served = m.decide(SYS, return_reason=True)
_check(act is not None and reason == "" and served == "Cerebras (m)",
       "primary 429 -> served by provider 2")
_check(counts.get("Groq") is None and counts.get("Cerebras") == 1 and counts.get("Google") is None,
       "Groq raised, Cerebras called once, Google not reached")

# whole chain 429 -> rate_limit, retry_after = max across chain.
m = _mkclient([_raise_ep("Groq", _Err("Error code: 429", {"retry-after": "10"})),
               _raise_ep("Cerebras", _Err("Error code: 429", {"retry-after": "25"})),
               _raise_ep("Google", _Err("Error code: 429", {"retry-after": "5"}))])
act, reason, ra, served = m.decide(SYS, return_reason=True)
_check(act is None and reason == "rate_limit" and ra == 25.0 and served == "",
       "whole chain 429 -> (None, rate_limit, max Retry-After, '')")

# dead last-resort link (expired key) is skipped, primary still works (stays primary).
counts = {}
m = _mkclient([_count_ep("Groq", counts), _count_ep("Cerebras", counts)])
act, reason, ra, served = m.decide(SYS, return_reason=True)
_check(act is not None and served == "Groq (m)" and counts.get("Cerebras") is None,
       "primary healthy -> stays on primary, chain not traversed")

# dead link reached only when earlier links fail: Groq 429 -> Cerebras expired -> Google ok
counts = {}
g = _raise_ep("Groq", _Err("Error code: 429", {"retry-after": "10"}))
c = _raise_ep("Cerebras", _Err("API key expired", {}))
go = _count_ep("Google", counts)
m = _mkclient([g, c, go])
act, reason, ra, served = m.decide(SYS, return_reason=True)
_check(act is not None and served == "Google (m)",
       "Groq 429, Cerebras expired-key -> fail over to Google")
_check(counts.get("Google") == 1, "Google served the request")

# parse error does NOT fail over (request-level); stays, no other provider tried.
counts = {}
def _bad_json(**kw):
    counts["Groq"] = counts.get("Groq", 0) + 1
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="not json at all"))])
bad = SimpleNamespace(provider="groq", model="m", label="Groq", display="Groq (m)",
                     client=SimpleNamespace(chat=SimpleNamespace(
                         completions=SimpleNamespace(create=_bad_json))))
m = _mkclient([bad, _count_ep("Cerebras", counts)])
act, reason, ra, served = m.decide(SYS, retries=0, return_reason=True)
_check(act is None and reason == "parse_error" and counts.get("Cerebras") is None,
       "parse error -> no failover (stays on provider)")

# single-endpoint 429 breaks after ONE call (no internal retry) -> rate_limit.
calls = {"n": 0}
def _one_429(**kw):
    calls["n"] += 1
    raise _Err("Error code: 429 - Rate limit", {"retry-after": "20"})
m = _mkclient([SimpleNamespace(provider="groq", model="m", label="Groq", display="Groq (m)",
             client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_one_429))))])
act, reason, ra, served = m.decide(SYS, retries=2, return_reason=True)
_check(act is None and reason == "rate_limit" and calls["n"] == 1,
       "single endpoint 429 -> rate_limit after ONE call")

# backward-compat: no return_reason -> bare dict on success.
m = _mkclient([_ok_ep("Groq")])
act2 = m.decide(SYS)
_check(isinstance(act2, dict) and act2["action"] == "done",
       "no return_reason -> bare dict (orchestrator path)")


# ── orchestrator._section_goal_unsupported ──────────────────────────────────
print("verifier section guard")
g = "System Settings is open on the Bluetooth pane"
_check(orch._section_goal_unsupported(g, "App: System Settings", "[1] BUTTON 'Search'") is True,
      "BT pane NOT achieved when Bluetooth off-screen -> guarded")
_check(orch._section_goal_unsupported(g, "App: System Settings\nWindow: Bluetooth",
      "[3] TOGGLE 'Bluetooth' (OFF)") is False,
      "BT pane actually shown -> not guarded")
_check(orch._section_goal_unsupported("Bluetooth is turned on", "App: System Settings",
      "[3] TOGGLE 'Bluetooth' (OFF)") is False, "toggle state goal not a section goal")
_check(orch._section_goal_unsupported("the Keyboard pane of System Settings is shown",
      "App: System Settings\nWindow: Bluetooth", "[3] TOGGLE 'Bluetooth' (OFF)") is True,
      "Keyboard pane claimed while on BT pane -> guarded")


# ── agent_core give-up + watchdog ───────────────────────────────────────────
print("rate-limit give-up + watchdog")
try:
    import agent_core as ac
except ModuleNotFoundError:
    print("  SKIP agent_core tests (PyObjC / macOS deps not available)")
else:
    _check(ac._rate_limit_should_abort(3, 0.0) is True, "3 consecutive -> abort")
    _check(ac._rate_limit_should_abort(2, 0.0) is False, "2 consecutive -> continue")
    _check(ac._rate_limit_should_abort(1, 95.0) is True, "backoff over 90 -> abort")
    _check(ac._rate_limit_should_abort(1, 60.0) is False, "backoff under 90 -> continue")
    _check(ac._watchdog_expired(200.0) is True, "200s no progress -> expired")
    _check(ac._watchdog_expired(100.0) is False, "100s no progress -> ok")

print("\nAll failover / give-up / verifier tests passed.")
