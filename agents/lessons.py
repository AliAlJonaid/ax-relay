"""
lessons.py — persistent, cross-task lessons (Reflexion memory that survives).
=============================================================================
A lesson is a short natural-language rule the agent learned (from its own mistake
or from the user teaching it) that should apply to EVERY future task — e.g.
"to open an app, use Spotlight: command+space → type → return".

Storage: ~/.config/computer-agent/lessons.json  (a JSON list of {text, source}).
Builtin lessons are always present (never written to disk). load() returns builtin
+ persisted; add() dedups (case-insensitive) and writes atomically. Robust to a
missing or corrupt file (treated as empty).

Both agent_core (inject at task start + persist new ones) and telegram_bridge
("lesson: <text>" command) import this module — it has NO heavy deps on purpose.
"""
from __future__ import annotations

import json
import os
import tempfile

LESSONS_DIR = os.path.expanduser("~/.config/computer-agent")
LESSONS_FILE = os.path.join(LESSONS_DIR, "lessons.json")
MAX_LESSONS = 60  # cap persisted (non-builtin) lessons; oldest pruned

# Rules the agent should NEVER forget — always injected, never written to disk.
BUILTIN: list[str] = [
    "To open ANY app reliably, use Spotlight: press command+space, type the app's "
    "real name (e.g. 'WhatsApp', 'Safari' — never a guessed variant like 'WhatsApp "
    "Desktop'), press return, then wait ~2s. The open_app action already does this "
    "(it resolves the real installed name and falls back to Spotlight automatically).",
    "Typing text into a field does NOT send or submit it. To actually send a message "
    "you must press the send button (or press return) AFTER typing, and the task is "
    "only done once the message has LEFT the input field and appears in the "
    "conversation. A field still containing the text is NOT sent.",
]


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def load() -> list[dict]:
    """All lessons: builtin first, then persisted (newest last). Missing/corrupt
    file → just the builtin set."""
    out: list[dict] = [{"text": t, "source": "builtin"} for t in BUILTIN]
    try:
        with open(LESSONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return out
    if isinstance(data, list):
        out.extend(x for x in data if isinstance(x, dict) and str(x.get("text", "")).strip())
    return out


def load_texts() -> list[str]:
    return [str(x["text"]).strip() for x in load() if str(x.get("text", "")).strip()]


def add(text: str, source: str = "auto") -> bool:
    """Append a lesson (dedup vs all existing, case-insensitive) and persist it.
    Returns True if added, False if it was a duplicate/empty. Atomic write."""
    text = (text or "").strip()
    if not text:
        return False
    existing = load()
    if _norm(text) in {_norm(x["text"]) for x in existing}:
        return False
    persisted = [x for x in existing if x.get("source") != "builtin"]
    persisted.append({"text": text, "source": source})
    cap = max(0, MAX_LESSONS - len(BUILTIN))
    persisted = persisted[-cap:] if cap else []
    _atomic_write(persisted)
    return True


def _atomic_write(persisted: list[dict]) -> None:
    try:
        os.makedirs(LESSONS_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=LESSONS_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(persisted, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LESSONS_FILE)
    except OSError:
        pass  # best-effort; the lesson still lives in-memory for this run


def format_block(texts: list[str]) -> str:
    """Render lessons as a prompt block. Returns '' when there's nothing to say."""
    texts = [t for t in (texts or []) if t]
    if not texts:
        return ""
    return ("DURABLE LESSONS (learned across past sessions — apply ALWAYS, never "
            "violate these):\n" + "\n".join(f"  - {t}" for t in texts))
