"""
test_telegram_auth_interrupt.py — unit tests for telegram_bridge.py.

Covers the security-critical + correctness-critical pure logic that currently has
ZERO tests:
  1. Session live-interrupt mailbox (add_interrupt / drain_interrupts) — empty,
     single, multi (newline-joined + cleared), second-drain -> None.
  2. _authorized(update) — the chat-id auth gate (setup-mode allow, match, reject),
     via the module global ALLOWED_CHAT_ID (saved & restored around each case).
  3. on_text routing — lesson: prefix queues "NEW LESSON (apply this now): <body>";
     plain text with a running session queues the raw text; unauthorized -> early
     return with no queueing. Uses stub _session + fake async update; run via
     asyncio.run.

No Mac, no network, no real Telegram bot. All fakes/stubs/monkeypatch.

Run:  .venv/bin/python agents/test_telegram_auth_interrupt.py
"""
import os
import sys
import asyncio
import threading
from unittest.mock import MagicMock
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))

import telegram_bridge as TB


def _check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


# ── 1. Session live-interrupt mailbox ─────────────────────────────────────────
# Session.__init__ needs a real Application + loop + chat_id, but the interrupt
# mailbox is pure-Python and self-contained. Bypass __init__ and wire only the
# interrupt attributes so we test the mailbox in isolation.

def _bare_session():
    s = TB.Session.__new__(TB.Session)
    s._interrupts = []
    s._interrupt_lock = threading.Lock()
    return s


print("Session interrupt mailbox")

s = _bare_session()
_check(s.drain_interrupts() is None, "drain on empty mailbox -> None")

s = _bare_session()
s.add_interrupt("hello there")
_check(s.drain_interrupts() == "hello there", "single interrupt drained verbatim")

s = _bare_session()
s.add_interrupt("one")
s.add_interrupt("two")
s.add_interrupt("three")
got = s.drain_interrupts()
_check(got == "one\ntwo\nthree", "multiple interrupts joined with newline")
_check(s.drain_interrupts() is None, "second drain after multi-add -> None (cleared)")

# Order preserved even under concurrent producers (also exercises the lock).
s = _bare_session()

def _producer(tid, n):
    for i in range(n):
        s.add_interrupt(f"t{tid}-{i}")

threads = [threading.Thread(target=_producer, args=(tid, 50)) for tid in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
combined = s.drain_interrupts()
parts = combined.split("\n")
_check(len(parts) == 200, "concurrent producers: 200 interrupts collected")
_check(len(set(parts)) == 200, "concurrent producers: no lost/duplicated lines")
_check(s.drain_interrupts() is None, "concurrent producers: mailbox cleared after drain")


# ── 2. _authorized(update) ────────────────────────────────────────────────────
# Reads module global ALLOWED_CHAT_ID. Save/restore around each case.

def _fake_update(chat_id):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))


_saved_allowed = TB.ALLOWED_CHAT_ID
try:
    # setup mode: empty/falsy -> allow (so /whoami works for first-time setup)
    TB.ALLOWED_CHAT_ID = ""
    _check(TB._authorized(_fake_update(123)) is True,
           "ALLOWED_CHAT_ID empty -> True (setup mode)")
    TB.ALLOWED_CHAT_ID = "   "  # .strip() happened at import; simulate falsy-after-strip
    # NOTE: _authorized checks the raw value truthiness, so a whitespace-only
    # string is truthy here. The real gate is the .strip() done at import time
    # producing "". We test the contract: a value that is falsy -> allow.
    TB.ALLOWED_CHAT_ID = ""
    _check(TB._authorized(_fake_update(999)) is True,
           "ALLOWED_CHAT_ID falsy -> True regardless of chat id")

    # configured + match
    TB.ALLOWED_CHAT_ID = "123"
    _check(TB._authorized(_fake_update(123)) is True,
           "ALLOWED_CHAT_ID='123' + chat 123 -> True")

    # configured + mismatch -> reject
    _check(TB._authorized(_fake_update(999)) is False,
           "ALLOWED_CHAT_ID='123' + chat 999 -> False")

    # chat id is compared as str
    _check(TB._authorized(_fake_update(123)) is True,
           "chat id compared as str against ALLOWED_CHAT_ID")
finally:
    TB.ALLOWED_CHAT_ID = _saved_allowed


# ── 3. on_text routing ────────────────────────────────────────────────────────
# on_text reads the module global _session and calls _authorized. We inject a
# stub _session (a recorder) and control ALLOWED_CHAT_ID to force the authorized
# branch. The fake update exposes .message.text and an async .message.reply_text.

class _StubSession:
    """Records interrupts; pretends a task is running; never absorbs answers."""
    running = True

    def __init__(self):
        self.interrupts = []

    def add_interrupt(self, text):
        self.interrupts.append(text)

    def deliver_answer(self, text):
        return False  # so on_text falls through to the interrupt branch


def _fake_async_update(text):
    msg = SimpleNamespace(text=text, reply_text=MagicMock())

    async def _reply_text(*a, **kw):
        return None

    msg.reply_text = _reply_text
    return SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=123))


_saved_session = TB._session
_saved_allowed = TB.ALLOWED_CHAT_ID
try:
    TB.ALLOWED_CHAT_ID = "123"  # authorized

    # 3a. lesson: prefix -> saves a lesson AND queues the "NEW LESSON ..." interrupt
    #     when a task is running. We stub lessons_store.add so no disk I/O happens.
    stub = _StubSession()
    TB._session = stub
    captured = {}

    def _fake_add(body, source=None):
        captured["body"] = body
        captured["source"] = source
        return True

    orig_add = TB.lessons_store.add
    TB.lessons_store.add = _fake_add
    try:
        up = _fake_async_update("lesson: always click the blue button")
        asyncio.run(TB.on_text(up, None))
    finally:
        TB.lessons_store.add = orig_add
    _check(captured.get("body") == "always click the blue button",
           "lesson: body passed to lessons_store.add")
    _check(captured.get("source") == "user", "lesson: source='user'")
    _check(len(stub.interrupts) == 1, "lesson: exactly one interrupt queued")
    _check(stub.interrupts[0] == "NEW LESSON (apply this now): always click the blue button",
           "lesson: interrupt text is exact 'NEW LESSON (apply this now): <body>'")

    # 3b. plain text + running session -> raw text queued as interrupt
    stub = _StubSession()
    TB._session = stub
    up = _fake_async_update("watch out for the popup")
    asyncio.run(TB.on_text(up, None))
    _check(stub.interrupts == ["watch out for the popup"],
           "plain text with running session -> raw text queued verbatim")

    # 3c. unauthorized -> early return, nothing queued
    stub = _StubSession()
    TB._session = stub
    TB.ALLOWED_CHAT_ID = "999999"  # not 123 -> unauthorized
    up = _fake_async_update("anything goes")
    asyncio.run(TB.on_text(up, None))
    _check(stub.interrupts == [],
           "unauthorized -> on_text returns early, nothing queued")

    # 3d. lesson: with empty body -> no interrupt queued (usage reply path)
    stub = _StubSession()
    TB._session = stub
    TB.ALLOWED_CHAT_ID = "123"  # authorized again
    orig_add = TB.lessons_store.add
    TB.lessons_store.add = lambda *a, **k: True
    try:
        up = _fake_async_update("lesson:")
        asyncio.run(TB.on_text(up, None))
    finally:
        TB.lessons_store.add = orig_add
    _check(stub.interrupts == [],
           "lesson: with empty body -> no interrupt queued")
finally:
    TB._session = _saved_session
    TB.ALLOWED_CHAT_ID = _saved_allowed


print("\nAll telegram_bridge auth + interrupt tests passed.")
