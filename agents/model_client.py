"""
model_client.py — Model-agnostic LLM client (Layer 4)
=====================================================
Talks to ANY OpenAI-API-compatible model. The provider and model are read from
.env, so switching models is a one-line change — no code edits.

Core principle enforced here: the model NEVER outputs coordinates. The system
prompt forbids it. The model only picks an element NUMBER from the list that
perception (ax_tree.py) produced, plus an action.

Supported providers out of the box (all via the OpenAI SDK):
  • openrouter  → https://openrouter.ai/api/v1     (default: free, cloud, no heat)
  • local       → http://localhost:11434/v1        (Ollama)
  • google      → Google AI Studio OpenAI-compat endpoint
  • custom      → set OPENAI_BASE_URL yourself

.env keys:
  AGENT_PROVIDER=openrouter            # openrouter | local | google | custom
  AGENT_MODEL=qwen/qwen3.6-plus:free   # any model id for the chosen provider
  OPENROUTER_API_KEY=...
  GEMINI_API_KEY=...                   # used when AGENT_PROVIDER=google
  OPENAI_BASE_URL=...                  # used when AGENT_PROVIDER=custom
  OPENAI_API_KEY=...                   # used when AGENT_PROVIDER=custom
"""

from __future__ import annotations

import os
import re
import json
import time
import base64
from typing import Any, Optional

from openai import OpenAI


# ── Provider registry + failover chain ──────────────────────────────────────
# Every provider speaks the OpenAI Chat Completions API, so one client type serves
# them all — only base_url + key + model id differ per link. AGENT_PROVIDER_CHAIN
# sets an ordered failover list; if it's empty we fall back to the single
# AGENT_PROVIDER/AGENT_MODEL (unchanged behaviour).

_PROVIDERS = {
    "groq":       {"base_url": "https://api.groq.com/openai/v1",
                   "key_env": "GROQ_API_KEY", "default": "openai/gpt-oss-120b",
                   "label": "Groq"},
    "cerebras":   {"base_url": "https://api.cerebras.ai/v1",
                   "key_env": "CEREBRAS_API_KEY", "default": "llama-3.3-70b",
                   "label": "Cerebras"},
    "google":     {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                   "key_env": "GEMINI_API_KEY", "default": "gemini-2.0-flash",
                   "label": "Google"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY", "default": "qwen/qwen3.6-plus:free",
                   "label": "OpenRouter"},
    "zenmux":     {"base_url": "https://zenmux.ai/api/v1",
                   "key_env": "ZENMUX_API_KEY", "default": "z-ai/glm-5.2-free",
                   "label": "ZenMux"},
    "local":      {"base_url": "http://localhost:11434/v1",
                   "key_env": None, "default": "qwen2.5vl:7b",
                   "label": "Ollama (local)"},
    "custom":     {"base_url_env": "OPENAI_BASE_URL",
                   "key_env": "OPENAI_API_KEY", "default": "gpt-4o-mini",
                   "label": "Custom"},
}


class _Endpoint:
    """One link in the provider chain: an OpenAI client + model id + labels."""
    __slots__ = ("provider", "model", "client", "label", "display")

    def __init__(self, provider, model, client, label, display):
        self.provider = provider      # "groq"
        self.model = model            # "openai/gpt-oss-120b"
        self.client = client          # OpenAI(...)
        self.label = label            # "Groq"
        self.display = display        # "Groq (openai/gpt-oss-120b)"


def _parse_chain(chain_str: str) -> list[tuple[str, str]]:
    """'groq:openai/gpt-oss-120b,cerebras:gpt-oss-120b' -> [(provider, model), ...].
    A bare provider name (no ':model') uses that provider's default model."""
    out: list[tuple[str, str]] = []
    for item in chain_str.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            prov, model = item.split(":", 1)
        else:
            prov, model = item, ""
        out.append((prov.strip().lower(), model.strip()))
    return out


def _chain_display(endpoints: list["_Endpoint"]) -> str:
    """Human-readable failover chain for Telegram (e.g. Groq → Cerebras → +8 more)."""
    if not endpoints:
        return ""
    if len(endpoints) <= 4:
        return " → ".join(ep.label for ep in endpoints)
    head = " → ".join(ep.label for ep in endpoints[:3])
    return f"{head} → +{len(endpoints) - 3} more"


def _make_endpoint(provider: str, model: str) -> _Endpoint:
    """Build one endpoint, reading its base_url + key from env."""
    cfg = _PROVIDERS.get(provider)
    if not cfg:
        raise RuntimeError(f"Unknown provider '{provider}' (known: "
                           f"{', '.join(sorted(_PROVIDERS))})")
    if "base_url_env" in cfg:
        base = os.environ.get(cfg["base_url_env"], "").strip()
        if not base:
            raise RuntimeError(f"{cfg['base_url_env']} missing in .env (custom provider)")
    else:
        base = cfg["base_url"]
    if cfg["key_env"] is None:
        key = "ollama"  # local Ollama needs no real key
    else:
        key = os.environ.get(cfg["key_env"], "").strip()
        if not key:
            raise RuntimeError(f"{cfg['key_env']} missing in .env (provider {provider})")
    model = model or cfg["default"]
    # A modest client-level timeout bounds /models validation calls; the chat
    # create() call passes its own (longer) timeout explicitly. max_retries=0 so a
    # rate-limited/down primary raises at once and we fail over to the next link
    # WITHOUT the SDK's multi-second Retry-After backoff (big per-step speedup when
    # the primary is throttling). Our chain owns retry via failover.
    client = OpenAI(api_key=key, base_url=base, timeout=12.0, max_retries=0)
    return _Endpoint(provider, model, client, cfg["label"], f"{cfg['label']} ({model})")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _find_model_id(want: str, available: list[str]) -> Optional[str]:
    """Find `want` in a provider's model catalog, tolerating org prefixes / version
    suffixes / punctuation. Exact → normalized-equal → normalized-containment →
    token overlap. Returns None if nothing reasonable matches."""
    if not want or not available:
        return None
    if want in available:
        return want
    nwant = _norm(want)
    for a in available:
        if _norm(a) == nwant:
            return a
    for a in available:
        if nwant and nwant in _norm(a):
            return a
    tokens = [t for t in re.split(r"[^a-z0-9]+", want.lower()) if t]
    full = [a for a in available if tokens and all(tok in _norm(a) for tok in tokens)]
    if full:
        return sorted(full, key=len)[0]  # shortest = most canonical match
    return None


def _google_native_models(api_key: str) -> tuple[Optional[list[str]], str]:
    """Enumerate Google's generateContent-capable models via the NATIVE API. The
    OpenAI-compat /models endpoint is unreliable for Google, so we use the native
    one. Returns (ids, error_str); error_str is '' on success."""
    import urllib.request
    import urllib.parse
    import urllib.error
    import json
    url = ("https://generativelanguage.googleapis.com/v1beta/models?key="
           + urllib.parse.quote(api_key) + "&pageSize=200")
    try:
        data = json.load(urllib.request.urlopen(url, timeout=12))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:120]}"
    except Exception as e:
        return None, str(e)[:120]
    ids = [
        m.get("name", "").replace("models/", "")
        for m in data.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", ["generateContent"])
    ]
    return ids, ""


def _validate_model_id(ep: _Endpoint) -> None:
    """Confirm ep.model exists in its catalog and correct it in place if it differs
    (e.g. a version suffix or a renamed Gemini id). Also surfaces a clearly dead
    link (expired/invalid key). Best-effort: a catalog we can't read keeps the id."""
    ids: Optional[list[str]] = None
    err = ""
    if ep.provider == "google":
        # Google's OpenAI-compat /models is unreliable — enumerate via native API.
        key = os.environ.get(_PROVIDERS["google"]["key_env"], "")
        ids, err = _google_native_models(key) if key else (None, "no key")
    if ids is None:
        try:
            ids = [m.id for m in ep.client.models.list()]
        except Exception as e:
            err = err or str(e)[:80]
            print(f"  ⚠️  {ep.label}: can't read model catalog ({err}) — "
                  f"left as '{ep.model}'; this failover link may be dead")
            return
    if ep.model in ids:
        return
    fixed = _find_model_id(ep.model, ids)
    if fixed:
        print(f"  ℹ️  {ep.label}: model '{ep.model}' not in catalog → corrected to '{fixed}'")
        ep.model = fixed
        ep.display = f"{ep.label} ({fixed})"
    else:
        print(f"  ⚠️  {ep.label}: model '{ep.model}' not found in catalog; using as-is")


def _build_endpoints() -> list[_Endpoint]:
    """Parse AGENT_PROVIDER_CHAIN (ordered failover chain) or fall back to the
    single AGENT_PROVIDER/AGENT_MODEL. Always returns a non-empty list."""
    chain_str = os.environ.get("AGENT_PROVIDER_CHAIN", "").strip()
    if chain_str:
        specs = _parse_chain(chain_str)
    else:
        specs = [(os.environ.get("AGENT_PROVIDER", "openrouter").strip().lower(),
                  os.environ.get("AGENT_MODEL", "").strip())]
    specs = specs or [("openrouter", "")]
    endpoints = [_make_endpoint(p, m) for p, m in specs]
    for ep in endpoints:
        _validate_model_id(ep)
    return endpoints


def _should_failover(err: str, exc: BaseException) -> tuple[bool, str, float]:
    """Did this failed call mean 'this provider is unavailable — try the next'
    (True) or 'a request-level problem — stop' (False)? Returns
    (failover, reason, retry_after_s).

    Fail over on provider-UNAVAILABLE errors: rate-limit/quota, bad/expired key,
    timeout, connection, 5xx. These are exactly the cases another provider can
    rescue. Do NOT fail over on payload/4xx errors (a bad request fails everywhere)
    or parse/empty replies (a model-output issue, identical on the same model
    elsewhere)."""
    low = err.lower()
    if _is_rate_limit(err):
        return True, "rate_limit", _retry_after_seconds(exc)
    # Auth / key problems (incl. Google's "API key expired", returned as a 400) —
    # this provider can't serve us; skip it like any unavailable link.
    if ("api key" in low or "api_key" in low or "unauthorized" in low
            or "permission denied" in low or re.search(r"error code: 40[13]", low)):
        return True, "auth", 0.0
    if "timed out" in low or "timeout" in low:
        return True, "timeout", 0.0
    if ("connection error" in low or "connection aborted" in low
            or "connection reset" in low or "connection refused" in low):
        return True, "connection", 0.0
    if (re.search(r"error code: 5\d\d", low) or "service unavailable" in low
            or "overloaded" in low or "internal server error" in low):
        return True, "server_error", 0.0
    return False, "error", 0.0


def _resolve_provider() -> tuple[OpenAI, str, str]:
    """Backward-compat shim: returns the PRIMARY endpoint's (client, model, label)."""
    ep = _build_endpoints()[0]
    return ep.client, ep.model, ep.label



# ── System prompt (NO coordinate guessing — this is the whole point) ────────

SYSTEM_PROMPT = """\
You are a precise macOS computer-use agent. You control the Mac by choosing
actions, ONE per turn.

THE MOST IMPORTANT RULE
=======================
You NEVER output screen coordinates. You do not estimate x/y positions. Instead,
you are given an ELEMENTS list: every interactive element on screen, each with a
NUMBER, its type, and its label. To click or type into something, you reference
its NUMBER. The system already knows the exact real position of each number.

If the element you need is NOT in the list, do NOT guess a position. Instead:
  • scroll to reveal it, or
  • open the right app/menu first, or
  • request a fresh look (the list refreshes every turn).

HOW YOU SEE THE SCREEN
======================
Each turn you receive:
  • A short context block (frontmost app, window title).
  • The ELEMENTS list. Each line is a NUMBER, a CONTROL KIND, the label, and — for
    toggles/checkboxes — the current STATE in parentheses, e.g.:
        [1] BUTTON "Search"
        [2] TYPE "Address"
        [3] TOGGLE "Bluetooth" (ON)
        [4] BUTTON "Submit" (disabled)
    Kinds: BUTTON, TOGGLE, CHECKBOX, RADIO, POPUP, LINK, TAB, TYPE, etc.
    (ON)/(OFF) is the REAL on-screen state, read from the OS — trust it.
    (disabled) means the control is greyed out and unusable — skip it.
  • Optionally a screenshot for situational awareness ONLY — never to read
    coordinates from. Coordinates always come from the numbered list.

ACTIONS (reply with EXACTLY ONE JSON object, no prose around it)
================================================================
Open / launch an app by its REAL name. open_app resolves the installed app and
falls back to Spotlight automatically — use the genuine name (e.g. "WhatsApp",
"Safari"), never a guessed variant ("WhatsApp Desktop"):
    {"thought": "...", "action": "open_app", "app": "WhatsApp"}
  (You may also launch via Spotlight yourself: press_key command+space, type, return.)

Click an element by its number:
    {"thought": "...", "action": "click_element", "element_id": 3}

Double-click an element:
    {"thought": "...", "action": "double_click_element", "element_id": 5}

Type text (into the currently focused field; click the field first if needed):
    {"thought": "...", "action": "type", "text": "Calgary restaurants"}

Press a single key or a chord:
    {"thought": "...", "action": "press_key", "key": "return"}
    {"thought": "...", "action": "press_key", "key": "command+l"}
    Allowed: return tab escape space delete backspace up down left right
             command+a command+c command+v command+t command+w command+l
             command+space

Scroll inside the current view:
    {"thought": "...", "action": "scroll", "direction": "down", "amount": 3}

Wait (for a page/app to load):
    {"thought": "...", "action": "wait", "duration": 2}

Send a screenshot to the user (Telegram) — use this for ANY "send/take screenshot"
request. Do NOT use command+shift+3/4/5 keyboard shortcuts (clipboard cannot be
verified and causes loops):
    {"thought": "...", "action": "send_screenshot"}

Ask the user a question (sent to them; you pause until they answer):
    {"thought": "...", "action": "ask_user", "question": "Which account should I use?"}

Finish — task done successfully:
    {"thought": "...", "action": "done", "summary": "what was accomplished"}

Give up — cannot proceed:
    {"thought": "...", "action": "failed", "summary": "why"}

RULES
=====
• Prefer click_element with a NUMBER. That is your main tool.
• To open or switch to an app, use open_app with the app's REAL name (e.g.
  "WhatsApp", "Safari", "System Settings") — never a guessed variant like
  "WhatsApp Desktop" or "WhatsApp Messenger". open_app resolves the real installed
  name and falls back to Spotlight (command+space) automatically; you may also drive
  Spotlight yourself (press_key command+space → type → return → wait).
• Typing into a field is NOT sending. To send/submit a message you must press the
  send button (click its number) or press_key return AFTER typing — and a send task
  is only done once the message has LEFT the input field and appears in the
  conversation. A field that still contains the text is NOT sent.
• To focus a browser URL bar: press_key command+l, then type the URL, then return.
• Click a text field (by its number) BEFORE you type into it.
• Never click the Dock. Never quit the Terminal/iTerm — that is where you run.
• If an action didn't change anything, try a different element number, scroll,
  or use the keyboard. Do not repeat the exact same failed action.
• TOGGLE / CHECKBOX / RADIO show their real state as (ON)/(OFF). To turn something
  ON or OFF, click that element's number ONCE — it flips the state. If it ALREADY
  shows the target state, you are DONE for that goal — do NOT click it again (you
  would just flip it back to where you started). Never click a (disabled) element.
• Think briefly in "thought" (one sentence). Then give the JSON. Output ONLY JSON.

FINISHING — RECOGNIZE YOU'VE ARRIVED AND STOP
=============================================
You are given a PLAN of numbered sub-goals and which one is CURRENT. Work ONLY on
the current sub-goal.

The MOMENT the screen already shows the goal state — the target app is frontmost,
the window title matches the requested section, or the requested element/result is
visible — you are DONE. Reply immediately with:
    {"thought": "...", "action": "done", "summary": "what was accomplished"}

Do NOT keep clicking, opening extra panels, re-searching, or "double-checking" once
the goal is visibly achieved. Wandering past the goal, repeating actions, or
opening detail/Show-Detail panels you were not asked for are FAILURES. When in
doubt and the goal looks reached, call done.
"""


# ── JSON extraction (robust to chatty models) ───────────────────────────────

def parse_action(raw: str) -> dict:
    """Extract the first valid JSON object from a model reply."""
    if not raw:
        raise ValueError("empty model reply")
    text = raw.strip()
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<thought>[\s\S]*?</thought>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Brace-matching fallback: find the first balanced {...}.
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                chunk = text[start:i + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    start = None
    raise ValueError(f"no valid JSON in reply: {text[:200]}")


# ── Rate-limit / backoff helpers ────────────────────────────────────────────

def _is_rate_limit(err: str) -> bool:
    """Does this error string look like a provider throttle (HTTP 429/quota)?"""
    low = err.lower()
    return "429" in err or "quota" in low or "rate" in low


def _retry_after_seconds(exc: BaseException, *, default: float = 20.0,
                         floor: float = 3.0, cap: float = 60.0) -> float:
    """
    Honor the provider's Retry-After when it 429s, so we back off on ITS schedule
    instead of a flat guess. Reads the header off the OpenAI SDK's underlying
    httpx response (every APIStatusError/RateLimitError carries `.response.headers`);
    falls back to `default` when absent or unparseable. Clamped to [floor, cap] so
    we neither hammer (too small) nor sleep forever (a huge server value).
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    val = None
    if headers:
        try:
            # httpx.Headers is case-insensitive; a plain dict may not be — try both.
            val = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:
            val = None
    if val is None:
        return default
    try:
        secs = float(val)
    except (TypeError, ValueError):
        return default  # HTTP-date form or unparseable — don't guess.
    return max(floor, min(cap, secs))


# ── Model client ────────────────────────────────────────────────────────────

class ModelClient:
    """Provider-neutral wrapper around one or more OpenAI-compatible chat models.
    With AGENT_PROVIDER_CHAIN set, holds an ordered failover chain and tries each
    provider in turn on rate-limit/timeout/5xx, staying on the primary while it
    works."""

    def __init__(self) -> None:
        self.endpoints = _build_endpoints()
        self.temperature = float(os.environ.get("AGENT_TEMPERATURE", "0.05"))
        self.max_tokens = int(os.environ.get("AGENT_MAX_TOKENS", "1024"))
        # Per-request hard timeout. Bounds a single create() call so a slow/hung
        # provider request can never freeze the agent (the no-progress watchdog
        # only fires between steps; this bounds the step itself).
        self.timeout = float(os.environ.get("AGENT_TIMEOUT_S", "30"))
        # Pace calls to stay under free-tier RPM limits (seconds between requests).
        self.min_call_interval = float(os.environ.get("AGENT_MIN_CALL_INTERVAL_S", "0"))
        self._last_call_at = 0.0
        # Stay on the last provider that worked — avoids Groq↔Cerebras flip-flop
        # every step and cuts Telegram noise + rate-limit churn.
        self._preferred_idx = 0
        # Primary endpoint (for code that reads model.label / model.model / model.client).
        primary = self.endpoints[0]
        self.client = primary.client
        self.model = primary.model
        self.label = primary.label
        # Compact chain for Telegram status (shows model id when label repeats).
        self.chain_display = _chain_display(self.endpoints)

    def describe(self) -> str:
        if len(self.endpoints) > 1:
            n = len(self.endpoints)
            return (f"{self.endpoints[0].label} :: {self.endpoints[0].model} "
                    f"(+ {n - 1} failover links)")
        return f"{self.label} :: {self.model}"

    def _try_endpoint(self, ep: _Endpoint, messages: list[dict],
                      retries: int) -> tuple[str, Optional[dict], str, float]:
        """Attempt ONE endpoint. Returns (status, action, reason, retry_after_s)
        where status is 'ok' | 'failover' | 'fail':
          • ok       → action parsed; this provider served the request.
          • failover → provider unavailable (429/timeout/conn/5xx); caller tries next.
          • fail     → request-level problem (parse/empty/4xx); caller stops.
        Parse failures re-ask the SAME provider up to `retries` times (self-correct)."""
        for _ in range(retries + 1):
            try:
                resp = ep.client.chat.completions.create(
                    model=ep.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout=self.timeout,
                )
                raw = resp.choices[0].message.content or ""
                if not raw.strip():
                    return "fail", None, "empty_reply", 0.0
                return "ok", parse_action(raw), "", 0.0
            except ValueError:
                # Parse failure — nudge once and re-ask the SAME provider.
                messages.append({
                    "role": "user",
                    "content": "Your last reply was not valid JSON. Reply with ONLY "
                               "one JSON action object.",
                })
                continue
            except Exception as e:
                failover, reason, ra = _should_failover(str(e), e)
                if failover:
                    return "failover", None, reason, ra
                return "fail", None, "error", 0.0
        return "fail", None, "parse_error", 0.0

    def decide(
        self,
        messages: list[dict],
        *,
        retries: int = 2,
        return_reason: bool = False,
    ):
        """
        Try the provider chain in order. On a rate-limit/timeout/5xx from the
        current provider, advance to the next and retry the SAME request — so we
        stay on the primary while it works, and only surface 'rate_limit' once the
        WHOLE chain is exhausted (letting agent_core's give-up/watchdog decide).
        `messages` must already include the system prompt as the first entry.

        return_reason=True → (action, reason, retry_after_s, served_display):
          • ""            → success (action is a dict; retry_after_s 0). served_display
                            names the provider that answered, e.g. "Cerebras (gpt-oss-120b)".
          • "rate_limit"  → EVERY provider was throttled/unavailable. retry_after_s is
                            the largest Retry-After seen across the chain.
          • "empty_reply"/"parse_error"/"error" → a request-level failure (did not
                            fail over); served_display is "".
        Without return_reason, returns the bare dict/None (orchestrator callers).
        """
        if self.min_call_interval > 0:
            gap = self.min_call_interval - (time.time() - self._last_call_at)
            if gap > 0:
                time.sleep(gap)
        last_reason = ""
        retry_after = 0.0
        n = len(self.endpoints)
        for attempt in range(n):
            idx = (self._preferred_idx + attempt) % n
            ep = self.endpoints[idx]
            status, action, reason, ra = self._try_endpoint(ep, messages, retries)
            if status == "ok":
                self._preferred_idx = idx
                self._last_call_at = time.time()
                return (action, "", 0.0, ep.display) if return_reason else action
            if status == "failover":
                # This provider is unavailable — remember why, try the next link.
                print(f"  ↺ {ep.label} {reason} → trying next provider")
                last_reason = reason
                retry_after = max(retry_after, ra)
                continue
            # status == 'fail': request-level problem. Don't fail over — another
            # provider (often the same model) won't fix a parse/empty/4xx error.
            print(f"  ⚠️  model.decide failed ({ep.display}): {reason}")
            return (None, reason, 0.0, "") if return_reason else None
        # Entire chain exhausted on provider-unavailable errors.
        print(f"  ⚠️  model.decide failed: whole chain unavailable (last: {last_reason})")
        return (None, last_reason or "rate_limit", retry_after, "") if return_reason else None



# ── Message construction helpers ────────────────────────────────────────────

def build_initial_messages() -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}]


def user_turn(
    *,
    task: str,
    context_block: str,
    elements_block: str,
    history_block: str = "",
    last_result: str = "",
    screenshot_b64: Optional[str] = None,
    mime: str = "image/jpeg",
    first_turn: bool = False,
) -> dict:
    """
    Build one user message. Screenshot is optional and for awareness only.
    """
    parts: list[dict] = []
    if screenshot_b64:
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{screenshot_b64}"},
        })

    if first_turn:
        text = (
            f"TASK: {task}\n\n"
            f"{context_block}\n\n"
            f"ELEMENTS (pick by number):\n{elements_block}\n\n"
            f"Issue your first action as a single JSON object."
        )
    else:
        text = (
            f"Result of last action: {last_result}\n\n"
            f"{context_block}\n\n"
            f"ELEMENTS (pick by number):\n{elements_block}\n"
            f"{history_block}\n\n"
            f"Continue the task: {task}\n"
            f"Reply with one JSON action object."
        )
    parts.append({"type": "text", "text": text})
    return {"role": "user", "content": parts}


def trim_messages(messages: list[dict], keep_images: int = 2, keep_turns: int = 8) -> list[dict]:
    """
    Keep the system message, strip old screenshots (token savings), and cap the
    conversation length. Mutates and returns the list.
    """
    # Strip images from all but the most recent `keep_images` image-bearing turns.
    img_idxs = [
        i for i, m in enumerate(messages)
        if isinstance(m.get("content"), list)
        and any(p.get("type") == "image_url" for p in m["content"])
    ]
    for i in img_idxs[:-keep_images] if len(img_idxs) > keep_images else []:
        messages[i]["content"] = [
            p for p in messages[i]["content"] if p.get("type") != "image_url"
        ] or [{"type": "text", "text": "(screenshot removed)"}]

    if len(messages) > keep_turns + 1:
        messages = [messages[0]] + messages[-keep_turns:]
    return messages
