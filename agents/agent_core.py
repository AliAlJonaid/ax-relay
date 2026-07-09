"""
agent_core.py — Phase 1 agent loop
==================================
Wires the layers together into a working agent (no Telegram yet — that's Phase 3).

Loop per turn:
    perceive (ax_tree)  →  think (model_client)  →  act (executor)  →  verify
                                                                         │
                                                  repeat until done/failed/max

What this proves in Phase 1: the model drives the Mac by picking element NUMBERS
from the deep accessibility tree. Zero coordinate guessing. The old "blind agent"
clicking random pixels is gone.

Run:
    python agent_core.py "open Safari, go to google.com, search for hiking trails"
Optional env (see model_client / .env):
    AGENT_PROVIDER, AGENT_MODEL, MAX_STEPS, etc.
"""

from __future__ import annotations

import os
import io
import re
import sys
import json
import time
import signal
import atexit
import base64
import hashlib
import subprocess
from typing import Optional

from dotenv import load_dotenv
from PIL import Image

# Local layers
import ax_tree
import model_client as mc
import orchestrator
import lessons as lessons_store
from executor import Executor, ActionResult

# Load .env from project root and agents/ — project .env uses override=True so
# edits (new keys, longer provider chain) apply on the next /task without
# restarting the bridge process.
load_dotenv(override=False)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)

# ── Config ──────────────────────────────────────────────────────────────────
MAX_STEPS = int(os.environ.get("MAX_STEPS", "40"))
SCREENSHOT_FOR_AWARENESS = os.environ.get("AGENT_SCREENSHOTS", "1") != "0"
# Phase 4: plan the task into sub-goals up front and verify/advance each step.
# Set AGENT_PLAN=0 to fall back to the plain Phase 1 loop.
USE_PLANNER = os.environ.get("AGENT_PLAN", "1") != "0"
# Whether the configured model can accept screenshots. Default OFF because the
# default model (Groq gpt-oss-120b) is TEXT-ONLY and rejects image content. We
# still CAPTURE screenshots (for the Telegram on_screenshot hook / awareness) —
# this only controls whether the image is also fed to the model. Set
# AGENT_MODEL_VISION=1 only when AGENT_MODEL is a vision model.
MODEL_VISION = os.environ.get("AGENT_MODEL_VISION", "0") != "0"
# Stuck detection: if the SAME action repeats with no change to the screen
# (window title + element list) this many times in a row, pause and ask the user.
STUCK_LIMIT = int(os.environ.get("STUCK_LIMIT", "3"))
# Rate-limit give-up: if a single step stays rate-limited this many times in a row,
# OR accumulates this much provider-mandated backoff, STOP the task (never let a
# throttled provider silently hang it for minutes).
RL_MAX_CONSECUTIVE = int(os.environ.get("RL_MAX_CONSECUTIVE", "3"))
RL_MAX_BACKOFF_S = float(os.environ.get("RL_MAX_BACKOFF_S", "90"))
# Reasons from model.decide() that mean the WHOLE chain couldn't serve the request
# (every provider rate-limited/down/bad-key). All route through the same back-off +
# give-up path; only repeated, total outages trigger an abort.
_UNAVAILABLE_REASONS = frozenset(
    {"rate_limit", "auth", "timeout", "connection", "server_error"})
# Overall no-progress watchdog: if no sub-goal advances AND no model call succeeds
# for this long, abort with a clear message instead of hanging.
WATCHDOG_S = int(os.environ.get("AGENT_WATCHDOG_S", "180"))
# Reflexion (verbal self-improvement, no training): on repeated failure the agent
# reflects on its own mistake and injects the lesson into later steps. Zero extra
# model calls on a clean, progressing run. AGENT_REFLEXION=0 disables it.
REFLEXION_ON = os.environ.get("AGENT_REFLEXION", "1") != "0"
REFLECT_AFTER = int(os.environ.get("REFLECT_AFTER", "1"))   # repeated actions → lesson
GOAL_LOOP_LIMIT = int(os.environ.get("GOAL_LOOP_LIMIT", "3"))  # → diagnose + ask user
REFLECTION_WINDOW = int(os.environ.get("REFLECTION_WINDOW", "4"))  # lessons kept in prompt
JPEG_QUALITY = 70
MAX_CAPTURE = 1600

# ── Session lock (ported from legacy) ───────────────────────────────────────
LOCK_DIR = os.path.expanduser("~/.config/computer-agent")
LOCK_FILE = os.path.join(LOCK_DIR, "session.lock")
_cleanup_done = False


def _acquire_lock() -> bool:
    os.makedirs(LOCK_DIR, exist_ok=True)
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            with open(LOCK_FILE) as f:
                data = json.load(f)
            os.kill(data["pid"], 0)  # raises if pid is dead
            print(f"  ⚠️  Another agent instance is running (PID {data['pid']}).")
            return False
        except (ProcessLookupError, OSError, json.JSONDecodeError, KeyError):
            try:
                os.unlink(LOCK_FILE)
            except OSError:
                pass
            return _acquire_lock()


def _release_lock() -> None:
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                data = json.load(f)
            if data.get("pid") == os.getpid():
                os.unlink(LOCK_FILE)
    except Exception:
        pass


def _cleanup() -> None:
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    _release_lock()


atexit.register(_cleanup)
signal.signal(signal.SIGINT, lambda *_: (_cleanup(), sys.exit(130)))
signal.signal(signal.SIGTERM, lambda *_: (_cleanup(), sys.exit(143)))


# ── macOS context ───────────────────────────────────────────────────────────

def _osa(script: str, timeout: int = 3) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def get_context_block() -> str:
    pid, name, bundle = ax_tree.get_frontmost_app()
    title = _osa('tell application "System Events" to get title of front window '
                 'of first application process whose frontmost is true')
    lines = []
    if name:
        lines.append(f"App: {name}")
    if title:
        lines.append(f"Window: {title}")
    # For browsers, the AX element list is a poor "did the page load?" signal
    # (autocomplete suggestions look like page content). The browser's REAL
    # current URL is ground truth — expose it so the verifier isn't fooled into
    # declaring a navigation "done" while the URL is still the old page.
    url = _browser_url(name, bundle)
    if url:
        lines.append(f"URL: {url}")
    return "\n".join(lines) if lines else "App: (unknown)"


def _browser_url(name: str, bundle: str) -> str:
    """Live current URL of the frontmost browser tab, or '' if not a browser."""
    if not name:
        return ""
    if bundle in ax_tree._SAFARI_BUNDLES:
        return _osa(f'tell application "{name}" to get URL of front document')
    if bundle in ax_tree._BROWSER_BUNDLES:
        return _osa(f'tell application "{name}" to get URL of active tab of front window')
    return ""


# ── Screen capture (awareness only — never for coordinates) ─────────────────

def capture_screenshot_b64() -> Optional[tuple[str, str]]:
    if not SCREENSHOT_FOR_AWARENESS:
        return None
    try:
        subprocess.run(["screencapture", "-x", "-t", "png", "/tmp/_agent_screen.png"],
                       capture_output=True, timeout=10)
        img = Image.open("/tmp/_agent_screen.png")
        w, h = img.size
        if max(w, h) > MAX_CAPTURE:
            ratio = MAX_CAPTURE / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:
        return None


# ── Agent hooks ─────────────────────────────────────────────────────────────
# A small bundle of optional callbacks so a front-end (Telegram, Phase 3) can
# observe and steer the run WITHOUT changing the loop. When hooks is None the
# agent behaves exactly as in Phase 1 (console-only).

class AgentHooks:
    """
    All callbacks are optional. Defaults make the agent run headless/console.
      • on_progress(text)        → called each step with a human-readable line.
      • confirm(question)->bool  → destructive-action confirmation.
      • ask_user(question)->str  → blocking question, returns the user's answer.
      • on_screenshot(png_path)  → called when a fresh screenshot exists on disk.
      • should_stop()->bool      → return True to abort the run cleanly.
      • poll_interrupt()->str|None → return any new mid-task user guidance to apply
                                     (drains a mailbox); None when there's nothing.
    """

    def __init__(
        self,
        on_progress=None,
        confirm=None,
        ask_user=None,
        on_screenshot=None,
        should_stop=None,
        poll_interrupt=None,
    ):
        self.on_progress = on_progress or (lambda text: None)
        self.confirm = confirm or _console_confirm
        self.ask_user = ask_user or _console_ask
        self.on_screenshot = on_screenshot or (lambda p: None)
        self.should_stop = should_stop or (lambda: False)
        self.poll_interrupt = poll_interrupt or (lambda: None)


# ── Stuck-detection helpers ─────────────────────────────────────────────────

def _action_sig(action: dict) -> str:
    """Canonical signature of an action, for detecting exact repeats."""
    act = str(action.get("action", "")).lower().strip()
    detail = (action.get("element_id") if "element_id" in action
              else action.get("key") or action.get("text")
              or action.get("direction") or action.get("app") or "")
    return f"{act}:{detail}"


def _action_human(action: dict) -> str:
    """Short human description of an action for the 'I'm stuck' message."""
    act = str(action.get("action", "")).lower().strip()
    if act in ("click_element", "double_click_element"):
        return f"click element #{action.get('element_id')}"
    if act == "scroll":
        return f"scroll {action.get('direction', 'down')}"
    if act == "type":
        return f"type '{str(action.get('text', ''))[:20]}'"
    if act == "press_key":
        return f"press {action.get('key', '')}"
    if act == "open_app":
        return f"open {action.get('app', '')}"
    return act or "that action"


def _stuck_eval(
    act: str,
    action_sig: str,
    world_sig: str,
    prev_action_sig: Optional[str],
    prev_world_sig: Optional[str],
    streak: int,
    limit: int,
) -> tuple[int, bool]:
    """Pure stuck-detection rule (no I/O — unit-testable).

    A "stuck repeat" = the model chose the SAME action while the visible world
    (AX element list + window/app/URL) is UNCHANGED from the previous turn. After
    `limit` such repeats in a row we are stuck in a loop and should ASK the user
    (→ Telegram) instead of burning steps. Terminal/introspective actions
    (done / failed / ask_user) never count and reset the streak.

    Returns (new_streak, should_ask).
    """
    if act in ("done", "failed", "ask_user"):
        return 1, False
    if action_sig == prev_action_sig and world_sig == prev_world_sig:
        streak += 1
    else:
        streak = 1
    return streak, streak >= limit


def _rate_limit_should_abort(
    consecutive: int,
    cumulative_backoff: float,
    *,
    max_consecutive: int = RL_MAX_CONSECUTIVE,
    max_backoff: float = RL_MAX_BACKOFF_S,
) -> bool:
    """Pure give-up rule for a step stuck on provider rate-limiting (HTTP 429).
    Abort when EITHER it has failed `max_consecutive` times in a row OR the
    cumulative provider-mandated backoff on this step reaches `max_backoff`.
    Keeps a throttled provider from silently burning minutes. Unit-testable."""
    return consecutive >= max_consecutive or cumulative_backoff >= max_backoff


def _watchdog_expired(elapsed_since_progress: float, *,
                      limit: float = WATCHDOG_S) -> bool:
    """Pure no-progress watchdog: true when nothing has advanced (no sub-goal, no
    successful model call) for `limit` seconds. The backstop against any hang the
    rate-limit rule doesn't catch (e.g. a slow provider, a loop that never errors
    but never gets anywhere). Unit-testable."""
    return elapsed_since_progress >= limit


def _sleep_or_stop(seconds: float, should_stop) -> bool:
    """Sleep up to `seconds`; return True if should_stop fired."""
    if seconds <= 0:
        return should_stop()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if should_stop():
            return True
        time.sleep(min(0.5, deadline - time.monotonic()))
    return should_stop()


# ── Reflexion (verbal self-improvement) helpers ─────────────────────────────
# Pure, unit-testable building blocks for the goal-level loop guard + reflection
# memory + disabled-action set. All three are goal-scoped: they reset the moment a
# sub-goal advances (real, verified progress).

# Actions that "commit" to a sub-goal attempt (vs exploratory scroll/wait). Only
# committing actions feed the repeat counter, so legitimately repeated scrolling
# to find something never trips the guard.
COMMITTING_ACTIONS = frozenset(
    {"click_element", "double_click_element", "type", "open_app", "press_key"})


def _is_committing(action: dict) -> bool:
    return str(action.get("action", "")).lower().strip() in COMMITTING_ACTIONS


# Matches a Send/Submit button label (English word-boundary + Arabic forms). Used to
# flag that the agent performed a SEND action this run.
_SEND_BTN_RE = re.compile(r"\b(send|sent|submit)\b", re.IGNORECASE)
_AR_SEND_BTN = ("ارسال", "إرسال", "أرسل", "ارسل", "أرسله", "ابعث")


def _looks_like_send(name: str) -> bool:
    if not name:
        return False
    if _SEND_BTN_RE.search(name):
        return True
    return any(k in name for k in _AR_SEND_BTN)


def _reflexion_update(action_sig: str, tried_sigs: set, goal_repeats: int
                      ) -> tuple[set, int]:
    """Per-action accounting for the CURRENT sub-goal. A repeat of an action whose
    signature we've already seen on this goal bumps goal_repeats — that repetition
    IS the doubt signal the goal-level guard keys on (distinct actions that happen
    to repeat, e.g. clearing + re-entering a calculation). Returns (tried_sigs,
    goal_repeats). Unit-testable."""
    sigs = set(tried_sigs)
    if action_sig in sigs:
        return sigs, goal_repeats + 1
    sigs.add(action_sig)
    return sigs, goal_repeats


def _goal_loop_should_reflect(goal_repeats: int, reflected: bool, *,
                              after: int = REFLECT_AFTER) -> bool:
    """Write a lesson once repetition begins (fires once per goal-attempt-run).
    Unit-testable."""
    return goal_repeats >= after and not reflected


def _goal_loop_should_trip(goal_repeats: int, *, limit: int = GOAL_LOOP_LIMIT) -> bool:
    """Goal-level loop guard: trip when a sub-goal has been re-attempted (via
    repeated actions) past the limit with no verified progress. Unit-testable."""
    return goal_repeats >= limit


def _reflection_block(lessons: list[str], tried_actions: list[str], goal_repeats: int,
                      *, window: int = REFLECTION_WINDOW) -> str:
    """Build the prompt injection: past lessons (sliding window) + the disabled
    (already-tried) action set. The disabled set is surfaced ONLY once we're
    actually repeating (goal_repeats > 0) — so a clean first attempt isn't wrongly
    told its own steps 'failed'. Returns '' when there's nothing to inject.
    Unit-testable."""
    parts: list[str] = []
    if lessons:
        parts.append(
            "LESSONS FROM YOUR PAST MISTAKES (heed these — don't repeat them):\n"
            + "\n".join(f"  - {l}" for l in lessons[-window:]))
    if goal_repeats > 0 and tried_actions:
        parts.append(
            "You are REPEATING actions on this sub-goal without completing it "
            "(already tried: " + ", ".join(tried_actions[-6:]) +
            "). The goal may ALREADY be achieved on screen — re-read it and finish "
            "if so; otherwise take a genuinely different action.")
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


# ── The agent ───────────────────────────────────────────────────────────────

def run(task: str, hooks: "AgentHooks | None" = None) -> bool:
    if hooks is None:
        hooks = AgentHooks()

    if not _acquire_lock():
        print("Cannot start — another instance holds the lock.")
        hooks.on_progress("⛔ Another agent instance is already running.")
        return False

    try:
        model = mc.ModelClient()
    except Exception as e:
        print(f"Model setup failed: {e}")
        hooks.on_progress(f"⛔ Model setup failed: {e}")
        return False

    executor = Executor(confirm_cb=hooks.confirm)

    print("=" * 66)
    print("  COMPUTER-USE AGENT — Phase 1 (deep AX, number-based clicking)")
    print("=" * 66)
    print(f"  Task:     {task}")
    print(f"  Model:    {model.describe()}")
    print(f"  Chain:    {model.chain_display}")
    print(f"  Max steps:{MAX_STEPS}")
    print(f"  Abort:    slam the mouse into a screen corner")
    print("=" * 66)
    hooks.on_progress(f"🤖 {model.describe()} [{model.chain_display}]")

    # ── Phase 4: decompose the task into verifiable sub-goals up front ──────
    subgoals: list[str] = []
    cur_goal = 0
    if USE_PLANNER:
        try:
            subgoals = orchestrator.plan(model, task)
        except Exception as e:
            print(f"  (planner failed: {e}; continuing without a plan)")
            subgoals = []
        if subgoals:
            plan_str = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(subgoals))
            print(f"\n  📋 Plan ({len(subgoals)} sub-goals):\n{plan_str}")
            hooks.on_progress(f"📋 Plan ({len(subgoals)} sub-goals):\n{plan_str}")

    messages = mc.build_initial_messages()
    # Persistent (cross-task) lessons: inject once into the system prompt so they
    # apply EVERY turn. A rule learned once (by the agent or taught by the user) is
    # remembered across tasks. New lessons are appended via lessons_store.add().
    _durable = lessons_store.load_texts()
    if _durable:
        messages[0]["content"] += "\n\n" + lessons_store.format_block(_durable)
        print(f"  🧠 loaded {len(_durable)} durable lesson(s) "
              f"({lessons_store.LESSONS_FILE})")
    last_result = ""
    history: list[str] = []
    no_change_streak = 0
    # Live-interrupt + stuck-detection state.
    prev_action_sig: Optional[str] = None
    prev_world_sig: Optional[str] = None
    stuck_streak = 1
    fresh_guidance = ""   # user guidance to surface in the next THINK context
    # Rate-limit give-up accounting for the CURRENT step. Reset on any non-rate-
    # limit outcome (success or other failure).
    rl_consecutive = 0
    rl_backoff_total = 0.0
    # Which provider served the last successful call — used to surface failover
    # ("↪ switched to Cerebras"). None until the first success.
    last_provider_display: Optional[str] = None
    # The most recent app the agent opened — used to recover when perception goes
    # blind (0 elements): nudge the model to re-open THIS app, not flail blind.
    last_opened_app = ""
    # Send-task evidence (THIS run): a send/post/submit goal is only complete if the
    # agent ACTUALLY typed the text AND performed a send (return / Send button). This
    # is what stops false-done on pre-existing identical text.
    typed_this_run: list[str] = []
    sent_this_run = False
    screenshot_sent_this_run = False
    # No-progress watchdog clock (monotonic → immune to wall-clock jumps).
    last_progress_mono = time.monotonic()
    # Reflexion state, all scoped to the CURRENT sub-goal and reset when it advances.
    lessons: list[str] = []          # sliding window of natural-language lessons
    tried_sigs: set = set()          # action signatures already tried on this goal
    tried_actions: list[str] = []    # human descriptions of those (for the prompt)
    goal_repeats = 0                 # count of repeated (redundant) actions on goal
    reflected_this_goal = False      # write a lesson at most once per goal run
    # Baseline screen state (captured once at task start) for the stale-state guard:
    # a sub-goal only counts if the agent produced it THIS run, not pre-existing.
    baseline_context = ""
    baseline_elements = ""
    last_verified_sig: Optional[str] = None  # skip re-verifying an unchanged screen

    for step in range(1, MAX_STEPS + 1):
        print(f"\n── Step {step}/{MAX_STEPS} ──")

        # ── No-progress watchdog: bail with a clear message instead of hanging if
        # nothing has advanced (no sub-goal, no successful model call) for too long.
        # Because decide() never sleeps on a 429 internally and each HTTP request is
        # timeout-bounded, the loop spins often enough for this to actually fire.
        elapsed = time.monotonic() - last_progress_mono
        if _watchdog_expired(elapsed):
            abort = (f"❌ No progress for {int(elapsed)}s — aborting to avoid a hang "
                     f"(last: {(last_result or 'start')[:80]}). "
                     f"Try again, or switch model in .env.")
            print(f"\n  {abort}")
            hooks.on_progress(abort)
            _cleanup()
            return False

        # Allow a front-end to abort cleanly between steps.
        if hooks.should_stop():
            print("\n  🛑 Stopped by user.")
            hooks.on_progress("🛑 Stopped.")
            _cleanup()
            return False

        # ── Feature 1: live interrupt mailbox. Apply any mid-task guidance the
        # user sent, WITHOUT restarting. No-op in the console loop (poll_interrupt
        # defaults to None). Lets the planner revise sub-goals around the guidance.
        guidance = hooks.poll_interrupt()
        if guidance:
            print(f"  📨 interruption: {guidance[:80]}")
            gl = guidance.lower()
            override = any(k in gl for k in (
                "anyway", "do it", "send it", "follow", "my instruction",
                "execute", "i insist", "ارسلها", "أرسلها", "ارسلها", "نفّذ", "نفذ"))
            if override:
                print("  📣 explicit user command — overriding perceived state")
                hooks.on_progress("📣 executing your instruction")
            else:
                hooks.on_progress("📨 applying your guidance")
            kind = "explicit command" if override else "new guidance"
            prefix = ("The user is EXPLICITLY INSTRUCTING you to perform this action "
                      "NOW — execute it regardless of what you see on screen; do NOT "
                      "decide it's already done. ") if override else ""
            messages.append({"role": "user", "content":
                f"USER INTERRUPTION ({kind}, adjust course without restarting): "
                f"{prefix}{guidance}"})
            fresh_guidance = guidance
            stuck_streak = 1  # new guidance breaks any stuck loop
            if subgoals:
                try:
                    subgoals = orchestrator.revise(model, task, subgoals, guidance)
                    cur_goal = 0
                    plan_str = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(subgoals))
                    print(f"  📋 Revised plan:\n{plan_str}")
                    hooks.on_progress(f"📋 Revised plan:\n{plan_str}")
                except Exception as e:
                    print(f"  (revise failed: {e})")

        # 1) PERCEIVE
        elements = ax_tree.get_elements()
        elements_block = ax_tree.format_for_prompt(elements)
        context_block = get_context_block()
        print(f"  👁  {len(elements)} elements | {context_block.splitlines()[0]}")
        # World signature for stuck detection: window/app/url + visible elements.
        world_sig = hashlib.md5(
            (context_block + "\n" + elements_block).encode("utf-8")).hexdigest()
        # "Did the visible world change since last turn?" — keyed on the AX tree +
        # window/URL, NOT a screenshot hash. With AGENT_SCREENSHOTS=0 no fresh
        # screenshot is written each turn, so a file hash would be stale/empty and
        # the signal useless. world_sig is always valid and is what the hard
        # stuck-detector below uses too, so the soft nudge and the hard rule agree.
        if prev_world_sig and world_sig == prev_world_sig:
            no_change_streak += 1
        else:
            no_change_streak = 0

        # ── Phase 4: verify the current sub-goal against fresh perception and
        # advance. The instant ALL sub-goals are met, declare done and STOP —
        # this is what stops the agent wandering past its goal. ───────────────
        # Capture the task-start screen ONCE as the baseline for the stale-state
        # guard (a goal must be produced by the agent this run, not pre-existing).
        if step == 1 and not baseline_context and not baseline_elements:
            baseline_context = context_block
            baseline_elements = elements_block
        _prev_goal = cur_goal
        if subgoals:
            while cur_goal < len(subgoals) and orchestrator.verify(
                    model, subgoals[cur_goal], context_block, elements_block,
                    baseline_context, baseline_elements,
                    sent_this_run, bool(typed_this_run), screenshot_sent_this_run):
                print(f"  ✓ sub-goal {cur_goal + 1}/{len(subgoals)} done: {subgoals[cur_goal]}")
                hooks.on_progress(
                    f"✓ sub-goal {cur_goal + 1}/{len(subgoals)} done: {subgoals[cur_goal]}")
                cur_goal += 1
                last_progress_mono = time.monotonic()  # a goal advanced = progress
            if cur_goal >= len(subgoals):
                summary = f"All {len(subgoals)} sub-goals complete for: {task}"
                print(f"\n  🎉 DONE (verified): {summary}")
                hooks.on_progress(f"✅ Done: {summary}")
                _cleanup()
                return True
        if cur_goal != _prev_goal:
            # Real verified progress → clear the Reflexion state for the old goal.
            goal_repeats = 0
            tried_sigs = set()
            tried_actions = []
            reflected_this_goal = False

        # ── Reflexion: reflection memory + disabled-action set + goal-level loop
        # guard. Fires ONLY on doubt (repeated actions on a sub-goal with no
        # verified progress) — a couple of model calls per stuck goal, zero on a
        # clean run. Catches "doubt loops" the action-level detector misses (the
        # world changes each turn, so same-action detection never fires).
        if REFLEXION_ON and subgoals and cur_goal < len(subgoals):
            # (a) Write ONE lesson when repetition starts, injected into later steps.
            if _goal_loop_should_reflect(goal_repeats, reflected_this_goal):
                lesson = orchestrator.reflect(
                    model, task, subgoals[cur_goal], tried_actions,
                    context_block, elements_block, lessons)
                if lesson:
                    lessons.append(lesson)
                    lessons_store.add(lesson, source="auto")  # remember across tasks
                    print(f"  💡 reflection: {lesson[:100]}")
                reflected_this_goal = True
                last_progress_mono = time.monotonic()
            # (b) Goal-level loop guard: same sub-goal re-attempted past the limit.
            if _goal_loop_should_trip(goal_repeats):
                diag = orchestrator.diagnose(
                    model, task, subgoals[cur_goal], tried_actions,
                    context_block, elements_block, lessons) or {}
                dlesson = diag.get("lesson", "").strip()
                if dlesson:
                    lessons.append(dlesson)
                    lessons_store.add(dlesson, source="auto")  # remember across tasks
                    print(f"  💡 diagnosis: {dlesson[:100]}")
                    print(f"  💡 lesson: {dlesson[:120]}")
                last_progress_mono = time.monotonic()
                if diag.get("verdict") == "done_missed":
                    # The agent likely already succeeded — the verifier missed the
                    # evidence. Nudge it to read the screen and finish; don't loop.
                    fresh_guidance = (dlesson or ("The sub-goal appears already achieved "
                                                  "on screen — read the result and finish; "
                                                  "do not redo it."))
                    goal_repeats = 0
                    tried_actions = []
                    reflected_this_goal = False
                    last_result = "reflexion: goal likely already done; re-evaluate"
                    continue
                # Genuinely stuck — ask the user via Telegram instead of looping.
                q = (f"I've repeated {goal_repeats} action(s) on "
                     f"'{subgoals[cur_goal]}' without completing it. Am I missing "
                     f"something? (e.g. 'the result is element N', or 'stop')")
                print(f"\n  🧭 Goal-level loop guard tripped on "
                      f"'{subgoals[cur_goal]}' — asking the user.")
                hooks.on_progress(f"🧭 I keep failing '{subgoals[cur_goal]}'. {q}")
                ans = (hooks.ask_user(q) or "").strip()
                if ans.lower() in ("stop", "quit", "cancel", "abort", "halt", "no"):
                    print("\n  🛑 Stopped on user instruction (goal loop).")
                    hooks.on_progress("🛑 Stopped.")
                    _cleanup()
                    return False
                messages.append({"role": "user", "content":
                    f"USER GUIDANCE (you were looping on goal "
                    f"'{subgoals[cur_goal]}'): {ans}"})
                fresh_guidance = ans
                goal_repeats = 0
                tried_sigs = set()
                tried_actions = []
                reflected_this_goal = False
                last_result = f"You were looping on a goal; user guidance: {ans}"
                continue

        shot = capture_screenshot_b64()
        # Only notify the front-end when we ACTUALLY captured a fresh screenshot
        # this step (shot is not None). Guarding on a bare file-exists check would
        # stream a STALE png every step when AGENT_SCREENSHOTS=0 — defeating the
        # whole point of turning auto-capture off (heat/fan).
        if shot and os.path.exists("/tmp/_agent_screen.png"):
            hooks.on_screenshot("/tmp/_agent_screen.png")
        # Only feed the screenshot to the model if it's a vision model; a text-only
        # model (e.g. Groq gpt-oss-120b) errors on image content. The phone still
        # receives the screenshot above via on_screenshot regardless.
        model_shot = shot if MODEL_VISION else None

        # Build the history hint (last few actions).
        hist_block = ""
        if history:
            hist_block = "\nRecent actions:\n" + "\n".join(f"  {h}" for h in history[-5:])
        if no_change_streak >= 2:
            hist_block += (f"\n⚠️ Screen unchanged for {no_change_streak} turns — "
                           f"try a different element number, scroll, or the keyboard.")
        if len(elements) == 0:
            who = last_opened_app or "the app you are working in"
            hist_block += (f"\n🚨 BLIND: 0 elements visible — the frontmost app is NOT "
                           f"{who} (it lost focus / its window isn't accessible). Your "
                           f"VERY NEXT action must be open_app '{who}' to bring it back; "
                           f"any typing/keys now go to the WRONG app.")

        # 2) THINK
        plan_block = orchestrator.format_plan(subgoals, cur_goal) if subgoals else ""
        ctx_for_model = context_block + (("\n\n" + plan_block) if plan_block else "")
        # Reflexion injection: the agent's own past lessons + the disabled
        # (already-tried) action set, so it reads its mistakes and avoids them.
        ctx_for_model += _reflection_block(lessons, tried_actions, goal_repeats)
        if fresh_guidance:
            ctx_for_model += f"\n\nACTIVE USER GUIDANCE (follow this now): {fresh_guidance}"
            fresh_guidance = ""  # consumed for this turn (still in message history)
        msg = mc.user_turn(
            task=task,
            context_block=ctx_for_model,
            elements_block=elements_block,
            history_block=hist_block,
            last_result=last_result,
            screenshot_b64=model_shot[0] if model_shot else None,
            mime=model_shot[1] if model_shot else "image/jpeg",
            first_turn=(step == 1),
        )
        messages.append(msg)

        action, decide_reason, retry_after, served_by = model.decide(
            messages, return_reason=True)
        if action is None:
            messages.pop()
            if decide_reason in _UNAVAILABLE_REASONS:
                # The ENTIRE chain couldn't serve the request — decide() already
                # tried every provider (rate-limited / down / bad-key). Back off and
                # retry; give up only after repeated total outages. (decide() never
                # sleeps internally, so WE own the cadence; stuck-detection never
                # fires since no action was ever chosen.)
                rl_consecutive += 1
                rl_backoff_total += retry_after
                if _rate_limit_should_abort(rl_consecutive, rl_backoff_total):
                    abort = (f"❌ All providers unavailable — rate-limited or down "
                             f"(tried {model.chain_display}). Task aborted — try again "
                             f"in a few minutes, or check your API keys in .env.")
                    print(f"\n  {abort}")
                    hooks.on_progress(abort)
                    _cleanup()
                    return False
                msg = (f"⏳ All providers rate-limited ({model.chain_display}) — backing "
                       f"off, retry {rl_consecutive}/{RL_MAX_CONSECUTIVE}…")
                print(f"  {msg}")
                hooks.on_progress(msg)
                if _sleep_or_stop(retry_after, hooks.should_stop):
                    print("\n  🛑 Stopped by user.")
                    hooks.on_progress("🛑 Stopped.")
                    _cleanup()
                    return False
                last_result = "all providers rate-limited/unavailable; retrying"
            else:
                # Non-rate-limit failure (parse/empty/other): breaks the consecutive
                # rate-limit run, so a later total outage starts the count fresh.
                rl_consecutive = 0
                rl_backoff_total = 0.0
                time.sleep(0.5)
                last_result = f"model gave no usable action ({decide_reason}); retrying"
            continue

        # A real decision came back → some provider in the chain served it. Reset the
        # rate-limit accounting for this step and feed the no-progress watchdog.
        rl_consecutive = 0
        rl_backoff_total = 0.0
        last_progress_mono = time.monotonic()
        # Surface provider failover: if a DIFFERENT provider served this call than
        # the last, tell the phone (the primary recovered or we failed over).
        if served_by and served_by != last_provider_display:
            if last_provider_display is not None:
                print(f"  ↪ switched to {served_by}")
            last_provider_display = served_by

        # ── Feature 2: stuck detection. If the SAME action is chosen while the
        # world (window/app/url + AX elements) hasn't changed, STUCK_LIMIT times in
        # a row, pause and ASK the user (→ Telegram) instead of burning steps.
        # _stuck_eval is a pure helper so the rule is unit-testable in isolation.
        act_now = str(action.get("action", "")).lower().strip()
        action_sig = _action_sig(action)
        stuck_streak, is_stuck = _stuck_eval(
            act_now, action_sig, world_sig,
            prev_action_sig, prev_world_sig, stuck_streak, STUCK_LIMIT)
        prev_action_sig, prev_world_sig = action_sig, world_sig
        if is_stuck:
            stuck_streak = 1  # reset so we don't immediately re-fire next turn
            stuck_what = _action_human(action)
            q = (f"I seem stuck doing '{stuck_what}' — the screen isn't changing. "
                 f"What should I do? (e.g. 'click element N', 'the target is lower', "
                 f"or 'stop')")
            print(f"\n  ⚠️  Stuck repeating '{stuck_what}' — asking the user.")
            hooks.on_progress(f"⚠️ I seem stuck doing '{stuck_what}'. {q}")
            ans = (hooks.ask_user(q) or "").strip()
            messages.pop()  # drop this step's user_turn; we'll re-decide next step
            if ans.lower() in ("stop", "quit", "cancel", "abort", "halt", "no"):
                print("\n  🛑 Stopped on user instruction (stuck).")
                hooks.on_progress("🛑 Stopped.")
                _cleanup()
                return False
            messages.append({"role": "user", "content":
                f"USER GUIDANCE (you were stuck repeating '{stuck_what}'): {ans}"})
            fresh_guidance = ans
            last_result = f"You were stuck; user guidance: {ans}"
            continue

        messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})

        thought = action.get("thought", "")
        act = action.get("action", "?")
        print(f"  🧠 {thought[:90]}")
        print(f"  ▶  {act}" + (f" #{action['element_id']}" if "element_id" in action else "")
              + (f" '{action.get('text','')[:30]}'" if act == 'type' else "")
              + (f" {action.get('key','')}" if act == 'press_key' else ""))

        # Tell the front-end what we're about to do (one tidy line).
        detail = ""
        if "element_id" in action:
            detail = f" #{action['element_id']}"
        elif act == "type":
            detail = f" \"{action.get('text','')[:40]}\""
        elif act == "open_app":
            detail = f" {action.get('app') or action.get('app_name') or ''}"
        elif act == "press_key":
            detail = f" {action.get('key','')}"
        elif act == "send_screenshot":
            detail = ""
        hooks.on_progress(f"step {step}: {act}{detail} — {thought[:80]}")

        # 3) ACT
        result: ActionResult = executor.execute(action, elements)
        last_result = result.message
        print(f"  ✅ {result.message}" if result.ok else f"  ⛔ {result.message}")

        if result.deliver_screenshot and result.ok:
            screenshot_sent_this_run = True
            if os.path.isfile("/tmp/_agent_screen.png"):
                hooks.on_screenshot("/tmp/_agent_screen.png")

        history.append(f"{step}: {act} → {result.message[:60]}")

        # Remember the app we just opened, so a later BLIND (0-element) step can
        # nudge the model to re-open THIS app instead of flailing in the wrong one.
        if act == "open_app":
            last_opened_app = (action.get("app") or action.get("app_name")
                               or action.get("name") or last_opened_app)
        # Send-task evidence: did the agent TYPE and SEND this run? A send/post/submit
        # goal can't complete without both (prevents false-done on stale text).
        if act == "type":
            typed_this_run.append(str(action.get("text", "")))
        elif act == "press_key" and "return" in str(action.get("key", "")).lower():
            sent_this_run = True
        elif act in ("click_element", "double_click_element"):
            _eid = action.get("element_id")
            _el = next((e for e in elements if e.get("id") == _eid), None)
            if _el and _looks_like_send(_el.get("name", "")):
                sent_this_run = True

        # Reflexion accounting: record this committing action against the current
        # sub-goal. A repeat of an already-tried signature bumps goal_repeats — the
        # doubt signal the goal-level loop guard keys on. (Scroll/wait are
        # exploratory and don't count, so legitimate repeated scrolling never trips.)
        if (REFLEXION_ON and subgoals and cur_goal < len(subgoals)
                and _is_committing(action)):
            tried_sigs, goal_repeats = _reflexion_update(
                _action_sig(action), tried_sigs, goal_repeats)
            tried_actions.append(_action_human(action))

        # ask_user: routed through hooks (console in Phase 1, Telegram in Phase 3).
        if act == "ask_user" and result.question:
            ans = hooks.ask_user(result.question)
            messages.append({"role": "user", "content": f"User answered: {ans}"})
            last_result = f"user said: {ans}"

        # 4) VERIFY / terminate
        if result.terminal == "done":
            # Don't trust a self-declared done blindly. If there's a plan, force-
            # verify the remaining sub-goals NOW; accept ONLY if all are achieved OR
            # the FINAL (end-state) sub-goal is met. The final-goal fallback handles
            # transient intermediate goals (e.g. "field contains X" that's false again
            # after the message is sent) — the END state is what matters.
            accept = True
            if subgoals and cur_goal < len(subgoals):
                while cur_goal < len(subgoals) and orchestrator.verify(
                        model, subgoals[cur_goal], context_block, elements_block,
                        baseline_context, baseline_elements,
                        sent_this_run, bool(typed_this_run), screenshot_sent_this_run):
                    print(f"  ✓ sub-goal {cur_goal + 1}/{len(subgoals)} done (on done): "
                          f"{subgoals[cur_goal]}")
                    cur_goal += 1
                    last_progress_mono = time.monotonic()
                last_met = orchestrator.verify(model, subgoals[-1], context_block,
                                               elements_block, baseline_context,
                                               baseline_elements,
                                               sent_this_run, bool(typed_this_run),
                                               screenshot_sent_this_run)
                accept = cur_goal >= len(subgoals) or bool(last_met)
            if accept:
                print(f"\n  🎉 DONE: {result.message}")
                hooks.on_progress(f"✅ Done: {result.message}")
                _cleanup()
                return True
            # Premature done: the end-state isn't verified. Reject and make the agent
            # keep working instead of falsely succeeding (this is the send-vs-type /
            # stale-state fix biting at termination).
            reject = (f"⚠️ You called done, but the end-state sub-goal "
                      f"'{subgoals[-1]}' is NOT verified. Don't declare done until "
                      f"it's truly achieved (e.g. for a send, the message must appear "
                      f"as sent). Keep working on it.")
            print(f"\n  {reject}")
            messages.append({"role": "user", "content": reject + " Continue."})
            last_result = f"premature done rejected; '{subgoals[-1]}' not verified"
            continue
        if result.terminal == "failed":
            print(f"\n  ❌ FAILED: {result.message}")
            hooks.on_progress(f"❌ Failed: {result.message}")
            _cleanup()
            return False

        # Context hygiene
        messages = mc.trim_messages(messages)

    print(f"\n  ⏰ Reached max steps ({MAX_STEPS}).")
    _cleanup()
    return False


def _console_confirm(question: str) -> bool:
    try:
        return input(f"\n  ⚠️  {question} (y/n): ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _console_ask(question: str) -> str:
    """Default ask_user for the console/CLI path. EOF (no stdin, e.g. a background
    run) returns '' instead of crashing — the loop then continues with no guidance."""
    try:
        return input(f"\n  ❓ {question}\n     your answer: ").strip()
    except EOFError:
        return ""


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python agent_core.py "<task>"')
        print('Example: python agent_core.py "open Safari and search for the weather in Calgary"')
        sys.exit(1)

    task_arg = " ".join(sys.argv[1:])
    print("\n⚠️  This agent controls your mouse and keyboard.")
    print("    Abort anytime by slamming the mouse into a screen corner.\n")
    ok = run(task_arg)
    sys.exit(0 if ok else 1)
