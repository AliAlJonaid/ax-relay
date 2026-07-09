"""
orchestrator.py — Planner + Verifier (Layer 2, Phase 4)
=======================================================
Turns the "blind, wandering" agent into a goal-directed one.

  • plan(task)   → one model call that decomposes the task into the SHORTEST list
                   of concrete, individually-verifiable sub-goals.
  • verify(...)  → after each action, a lightweight model check: "is the CURRENT
                   sub-goal achieved?" using on-screen evidence (frontmost app,
                   window title, the visible element list). The agent advances
                   through the sub-goals and STOPS the moment the last one is met
                   — no meandering, no hitting the step cap.

Model-agnostic: it reuses the same ModelClient as the rest of the agent, so it
works with whatever provider/model is configured in .env (Groq, Gemini, …). It
keeps its OWN tiny message lists, so it never pollutes the main agent conversation.
"""

from __future__ import annotations

import re
from typing import Optional

import model_client as mc


# ── Prompts (kept tiny so the extra calls stay cheap/fast) ──────────────────

PLANNER_SYSTEM = """\
You are the PLANNER for a macOS computer-use agent. Decompose the user's task into
the SHORTEST sequence of concrete, individually-verifiable sub-goals.

Rules:
• Each sub-goal must describe an END STATE you can CHECK by looking at the screen —
  a frontmost app, a window title, a visible section/element, a field's content.
  Write states ("System Settings is open on the Bluetooth section"), not actions
  ("click Bluetooth").
• For on/off / enable / disable tasks, phrase the sub-goal as the desired STATE of
  the thing (e.g. "Bluetooth is turned off") — directly checkable from the screen.
• Each sub-goal must be a STABLE end-state that STAYS true once achieved — NEVER a
  transient intermediate state. Bad: "the input field contains X" (it's false again
  the moment you send/submit). For "type X and send/post/submit it", use ONE sub-goal
  describing the DELIVERED result: "X appears as a sent message in the chat". Do not
  split typing from sending into separate sub-goals.
• Use the FEWEST sub-goals that fully cover the task (usually 1-4). Do NOT pad.
• For screenshot tasks: the final sub-goal MUST be "Screenshot sent to the user"
  (the agent uses a send_screenshot action — NOT keyboard shortcuts like
  command+shift+3/4/5 and NOT "clipboard" states, which cannot be verified).
• Order them in the sequence they must happen.
• Reply with ONLY this JSON, nothing else:
  {"subgoals": ["first concrete sub-goal", "second", ...]}
"""

VERIFIER_SYSTEM = """\
You VERIFY whether ONE sub-goal of a macOS task is ALREADY achieved RIGHT NOW, using
only the evidence provided (frontmost app, WINDOW TITLE, the visible element list).

Be STRICT — the end state must already be true:
• "App is open / frontmost": the frontmost app must match the target.
• "A section/page/site is shown" (a Settings pane, a website, a result): the
  FRONTMOST APP MATCHING IS NOT ENOUGH — a named section is open ONLY when the
  WINDOW TITLE matches the target (e.g. the System Settings title is literally the
  section name — "Bluetooth" — when its pane is showing), or the current URL (shown
  as "URL:" for browsers) matches the target site, or the target's REAL content is
  visible. A section appearing as a SIDEBAR / row / link / search suggestion is NOT
  evidence the pane is open — that just means it's selectable. Example: "System
  Settings is open on the Bluetooth pane" is FALSE if the title is "Settings" or the
  screen shows the home/General pane, even if "Bluetooth" is listed in the sidebar.
• CRITICAL: text that was merely TYPED into a search box or address bar, or that
  appears as an autocomplete / search SUGGESTION, is NOT evidence the action
  completed. A URL typed into the address bar does NOT mean the site is open. For a
  browser goal, trust the "URL:" line — if it still shows the old/previous site
  (e.g. google.com when the goal was apple.com), the page has NOT loaded → false.
• For a "turn X on/off", "enable/disable X", or "X is on/off" goal: the GROUND TRUTH
  is the state printed on that control's element line — TOGGLE/CHECKBOX/RADIO elements
  carry (ON) or (OFF). Find the element whose label matches X and read that state:
  (ON)=on, (OFF)=off. If it shows the requested state → achieved; if the opposite, or
  X is not present → false. This is authoritative — do not require anything else.
• If the evidence is missing or ambiguous, answer false.
• STALE-STATE GUARD (critical): you are ALSO given the STATE AT TASK START (before
  the agent acted this run). The goal is achieved ONLY if the agent's OWN actions
  THIS run produced the target — NOT if the target text/state was already present at
  START. Compare CURRENT to START: if the goal-relevant content is UNCHANGED from
  START, answer false (the agent hasn't done it yet). e.g. if the message to "send"
  is already visible in the chat at START, seeing it NOW is NOT success — the agent
  must have produced a NEW, additional one.
• SEND vs TYPE: for a "send/transmit/submit/post/enter" goal, the content must have
  LEFT the input/compose field and appear in the conversation/feed/sent list. Text
  sitting in an input/compose field (not yet submitted) is NOT sent → false.

Reply with ONLY this JSON: {"achieved": true, "reason": "short evidence"}
or {"achieved": false, "reason": "what's missing"}.
"""


# ── Planner ──────────────────────────────────────────────────────────────────

def plan(model: mc.ModelClient, task: str) -> list[str]:
    """One model call → list of sub-goals. Degrades to [task] on any failure."""
    msgs = [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": f"TASK: {task}\n\nReturn the sub-goal JSON now."},
    ]
    out = model.decide(msgs)
    subs: list[str] = []
    if isinstance(out, dict):
        raw = out.get("subgoals") or out.get("sub_goals") or []
        if isinstance(raw, list):
            subs = [str(s).strip() for s in raw if str(s).strip()]
    if not subs:
        subs = [task]  # graceful fallback: the whole task as a single sub-goal
    return subs[:8]


# ── Re-planner (when the user sends mid-task guidance) ──────────────────────

REVISER_SYSTEM = """\
You are REVISING the PLAN for a macOS computer-use agent because the user sent new
mid-task GUIDANCE. Given the original task, the current sub-goals, and the guidance,
return an UPDATED sub-goal list: keep what still applies, and adjust / add / remove
only as the guidance requires. Keep it short, ordered, each a checkable END STATE.
If the guidance is just a hint (not a change of goal), return the sub-goals UNCHANGED.

Reply with ONLY this JSON: {"subgoals": ["...", ...]}
"""


def revise(model: mc.ModelClient, task: str, subgoals: list[str],
           guidance: str) -> list[str]:
    """Re-plan after user guidance. Falls back to the current sub-goals on failure."""
    cur = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(subgoals))
    user = (
        f"ORIGINAL TASK: {task}\n\nCURRENT SUB-GOALS:\n{cur}\n\n"
        f"USER GUIDANCE: {guidance}\n\nReturn the updated sub-goal JSON now."
    )
    out = model.decide([
        {"role": "system", "content": REVISER_SYSTEM},
        {"role": "user", "content": user},
    ])
    subs: list[str] = []
    if isinstance(out, dict):
        raw = out.get("subgoals") or out.get("sub_goals") or []
        if isinstance(raw, list):
            subs = [str(s).strip() for s in raw if str(s).strip()]
    return subs[:8] or list(subgoals)


# ── Verifier ─────────────────────────────────────────────────────────────────

# Phrases that mark a sub-goal as "a named section/pane/page is shown" (a
# navigation goal) — the goals prone to verifier false-positives ("Bluetooth pane
# is open" rubber-stamped from "App: System Settings" alone).
_SECTION_HINTS = (
    "pane", "section", "page", "tab", "is shown", "is displayed",
    "open on the", "open on a", "open to", "opened to", "opened on",
    "navigate to", "navigated to", "go to", "gone to",
)

# Generic words stripped before the remainder is treated as the section name we
# must actually see on screen — includes common app names so they can never
# satisfy a section goal by themselves.
_STOPWORDS = {
    "system", "settings", "app", "application", "window", "windows", "browser",
    "open", "opened", "opening", "shown", "showing", "shows", "display",
    "displayed", "loaded", "is", "are", "was", "were", "be", "been", "the", "a",
    "an", "on", "to", "of", "for", "and", "or", "in", "with", "frontmost",
    "section", "pane", "page", "tab", "navigate", "navigated", "go", "gone",
    "now", "currently",
}


def _section_goal_unsupported(subgoal: str, context_block: str,
                              elements_block: str) -> bool:
    """
    Conservative guard against verifier false-positives for "the X section/pane is
    shown" goals. If the sub-goal names a SPECIFIC section (e.g. "Bluetooth pane")
    but that name appears NOWHERE in the on-screen evidence (window title, URL,
    element labels), the section is clearly NOT shown → return True (unsupported).

    Only ever returns True (a hard reject). A goal it doesn't recognize, or one
    whose name IS somewhere on screen, is left to the model — so this can never
    create a NEW false-positive, only block the "rubber-stamp from the app name
    alone" kind. The model + VERIFIER_SYSTEM still handle the subtler "name is in
    the sidebar but the pane isn't open" case.
    """
    low_sub = subgoal.lower()
    if not any(h in low_sub for h in _SECTION_HINTS):
        return False  # not a section-style goal; nothing to guard
    candidates = [
        w for w in re.findall(r"[a-z0-9]+", low_sub)
        if len(w) >= 4 and w not in _STOPWORDS
    ]
    if not candidates:
        return False  # no specific name to require; let the model decide
    evidence = (context_block + "\n" + elements_block).lower()
    return not any(c in evidence for c in candidates)


def _truncate(s: str, n: int = 1500) -> str:
    s = s or ""
    return s if len(s) <= n else (s[:n] + f"\n…(+{len(s) - n} chars truncated)")


# A "send/post/submit" goal is an ACTION goal, not a state goal: success means the
# agent PERFORMED the send this run — never that matching text happens to be visible.
_SEND_RE = re.compile(r"\b(send|sent|submit|submitted|post|posted|publish)\b", re.IGNORECASE)
_AR_SEND = ("ارسال", "إرسال", "أرسل", "ارسل", "أرسله", "أرسلها", "ابعث", "أبعث")


def is_send_goal(subgoal: str) -> bool:
    """True for send/post/submit sub-goals (action goals), handled by a deterministic
    gate (not the loose LLM check). Word-boundary match for English (so 'consent'
    isn't a false hit), substring for Arabic forms."""
    if not subgoal:
        return False
    if _SEND_RE.search(subgoal):
        return True
    return any(k in subgoal for k in _AR_SEND)


_SCREENSHOT_RE = re.compile(
    r"\b(screenshot|screen[\s-]?shot|capture|لقطة|سكرين)\b", re.IGNORECASE)


def is_screenshot_goal(subgoal: str) -> bool:
    """True when the sub-goal requires delivering a screenshot to the user."""
    if not subgoal:
        return False
    if _SCREENSHOT_RE.search(subgoal):
        return True
    low = subgoal.lower()
    return "clipboard" in low and ("screen" in low or "capture" in low)


def verify(model: mc.ModelClient, subgoal: str, context_block: str,
           elements_block: str, baseline_context: str = "",
           baseline_elements: str = "", run_sent: bool = False,
           run_typed: bool = False, run_screenshot_sent: bool = False) -> bool:
    """Lightweight check: is `subgoal` achieved given the current evidence?
    `baseline_*` is the screen state at TASK START (stale-state guard). For a
    send/post/submit goal, `run_sent`/`run_typed` enforce that the agent ACTUALLY
    typed + sent THIS run — pre-existing identical text can never satisfy it."""
    # SEND-goal gate (deterministic, overrides the LLM): a send task is complete ONLY
    # if the agent executed a send action this run. This is the fix for the agent
    # seeing an old "مرحبا" on screen and declaring done without sending anything.
    if is_send_goal(subgoal) and not (run_sent and run_typed):
        return False
    # Screenshot gate: success only after send_screenshot action (clipboard/keyboard
    # shortcuts are invisible to the AX verifier and cause infinite loops).
    if is_screenshot_goal(subgoal) and not run_screenshot_sent:
        return False
    # Section guard: a named-section goal can't be achieved if its name isn't even on
    # screen — skips a wasted model call AND blocks the app-name-only false-positive.
    if _section_goal_unsupported(subgoal, context_block, elements_block):
        return False
    user = (
        f"SUB-GOAL: {subgoal}\n\n"
        f"STATE AT TASK START (BEFORE the agent acted — for the stale-state guard):\n"
        f"{_truncate(baseline_context)}\n"
        f"VISIBLE THEN:\n{_truncate(baseline_elements)}\n\n"
        f"CURRENT SCREEN:\n{context_block}\n\n"
        f"VISIBLE NOW:\n{_truncate(elements_block)}\n\n"
        f"AGENT ACTIONS THIS RUN: typed={run_typed}, sent (return/Send clicked)="
        f"{run_sent}.\n"
        f"Did the agent's OWN actions this run achieve the sub-goal (a NEW change vs "
        f"START)? Return the JSON now."
    )
    msgs = [
        {"role": "system", "content": VERIFIER_SYSTEM},
        {"role": "user", "content": user},
    ]
    out = model.decide(msgs)
    return bool(isinstance(out, dict) and out.get("achieved") is True)


# ── Reflexion (verbal self-improvement) ──────────────────────────────────────
# Two lightweight model calls, fired ONLY on failure/doubt (not every step). Both
# keep their own tiny message lists (never pollute the main conversation) and go
# through model.decide() → so they ride the failover chain like everything else.

REFLECTION_SYSTEM = """\
You are the REFLECTION voice of a macOS computer-use agent. The agent is repeating
work on a sub-goal without completing it, and is about to make the SAME mistake
again. In ONE concise sentence, state the LESSON: the specific mistake it keeps
making and what to do instead. Write in second person ("you").

Name the common anti-patterns explicitly when you see them:
• Re-typing / clearing a value it already entered, instead of READING the result
  that is already shown (a calculator display, a search result, a filled field).
  → "The result is already on screen; stop re-entering and read it, then finish."
• Re-clicking an element whose effect already happened.
• Re-opening something already open; redoing steps already done.

Ground the lesson in what is ACTUALLY on the current screen. Reply with ONLY:
{"lesson": "<one sentence>"}
"""

DIAGNOSE_SYSTEM = """\
You DIAGNOSE a macOS agent that is looping on one sub-goal. Decide: is the sub-goal
(a) ALREADY achieved on the current screen but the verifier missed the evidence, or
(b) genuinely NOT achieved yet? Look hard at the screen evidence (a computed result,
an open app, a toggle's state, a visible value).

Reply with ONLY:
{"verdict": "done_missed" | "stuck", "lesson": "<one sentence>", "reason": "<short>"}
• "done_missed" — the goal IS already visible on screen (say what proves it).
• "stuck" — it genuinely is not achieved.
"""


def _prior_lessons_block(lessons: list[str]) -> str:
    return "\n".join(f"  - {l}" for l in lessons[-3:]) if lessons else "  (none yet)"


def reflect(model: mc.ModelClient, task: str, subgoal: str, tried: list[str],
            context_block: str, elements_block: str, lessons: list[str]) -> str:
    """One model call → a short natural-language lesson about the mistake being
    repeated (Reflexion). Returns '' on any failure (caller simply skips)."""
    user = (
        f"TASK: {task}\n"
        f"SUB-GOAL (not yet completed): {subgoal}\n"
        f"ACTIONS ALREADY TRIED: {', '.join(tried[-6:]) or '(none)'}\n"
        f"PRIOR LESSONS:\n{_prior_lessons_block(lessons)}\n\n"
        f"CURRENT SCREEN:\n{context_block}\n\n"
        f"VISIBLE ELEMENTS:\n{elements_block}\n\n"
        f"What one-sentence lesson should the agent heed? Return the JSON now."
    )
    out = model.decide([
        {"role": "system", "content": REFLECTION_SYSTEM},
        {"role": "user", "content": user},
    ])
    return str(out.get("lesson", "")).strip() if isinstance(out, dict) else ""


def diagnose(model: mc.ModelClient, task: str, subgoal: str, tried: list[str],
             context_block: str, elements_block: str,
             lessons: list[str]) -> dict:
    """One model call → {'verdict': 'done_missed'|'stuck', 'lesson', 'reason'}.
    Called when the goal-level loop guard trips: 'am I failing, or did I miss the
    evidence?' Returns {} on failure (caller treats as stuck → asks the user)."""
    user = (
        f"TASK: {task}\n"
        f"SUB-GOAL: {subgoal}\n"
        f"ACTIONS TRIED (repeatedly, without completing it): {', '.join(tried[-6:]) or '(none)'}\n"
        f"PRIOR LESSONS:\n{_prior_lessons_block(lessons)}\n\n"
        f"CURRENT SCREEN:\n{context_block}\n\n"
        f"VISIBLE ELEMENTS:\n{elements_block}\n\n"
        f"Is the sub-goal already achieved on the current screen? Return the JSON now."
    )
    out = model.decide([
        {"role": "system", "content": DIAGNOSE_SYSTEM},
        {"role": "user", "content": user},
    ])
    if not isinstance(out, dict):
        return {}
    verdict = str(out.get("verdict", "")).strip().lower()
    if verdict not in ("done_missed", "stuck"):
        verdict = "stuck"
    return {
        "verdict": verdict,
        "lesson": str(out.get("lesson", "")).strip(),
        "reason": str(out.get("reason", "")).strip(),
    }


# ── Plan rendering for the agent's per-turn context ─────────────────────────

def format_plan(subgoals: list[str], cur: int) -> str:
    """Render the plan with progress markers, injected into the model context."""
    lines = ["PLAN (sub-goals):"]
    for i, s in enumerate(subgoals):
        mark = "[x]" if i < cur else ("[>]" if i == cur else "[ ]")
        tag = "   <- CURRENT" if i == cur else ""
        lines.append(f"  {mark} {i + 1}. {s}{tag}")
    lines.append(
        "Work ONLY on the CURRENT sub-goal. The moment every sub-goal is [x] "
        "(the screen already shows the goal state), reply with the done action "
        "and a one-line summary — do not keep exploring."
    )
    return "\n".join(lines)
