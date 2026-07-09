"""
telegram_bridge.py — Remote control over Telegram (Layer 1, Phase 3)
====================================================================
Lets you drive the agent from your phone while away from the Mac:

  /task <description>   start a task (the agent runs and reports progress)
  /status               what the agent is doing right now
  /stop                 abort the current task
  /screen               send a fresh screenshot of the Mac
  /send <path>          send yourself a file from the Mac
  /whoami               print your Telegram chat id (for first-time setup)
  (plain text reply)    answered to the agent when it asks a question / confirms

Security: only the chat id in TELEGRAM_ALLOWED_CHAT_ID may control the agent.
If that value is missing, control commands fail closed and only /whoami remains
available for first-time setup.

Design note — threading:
  The agent loop (agent_core.run) is synchronous and drives the mouse/keyboard.
  Telegram runs an asyncio event loop. So we run the agent in a background
  THREAD, and its hooks marshal messages back onto the asyncio loop via
  run_coroutine_threadsafe. Confirmations/questions block the worker thread on a
  threading.Event until the user replies in chat.

Run:
  python telegram_bridge.py            # start the bot
  python telegram_bridge.py --whoami   # helper: prints your chat id when you message the bot
"""

from __future__ import annotations

import os
import sys
import json
import time
import atexit
import fcntl
import asyncio
import threading
from typing import Optional

from dotenv import load_dotenv

# Load .env from project root and agents/ — project .env uses override=True so
# key/chain edits take effect without restarting the bridge.
load_dotenv(override=False)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import agent_core
import lessons as lessons_store


# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()


# ── Single-instance guard ───────────────────────────────────────────────────
# Telegram allows only ONE long-poller per bot token. A second bridge process
# makes the bot return 409 Conflict and updates stop arriving reliably to EITHER
# process — which is exactly why /task messages were getting delayed/ignored.
# We refuse to start if another live instance holds this lock.
#
# Implementation: an exclusive flock on a lock file. flock is released by the OS
# the instant the process dies (even via SIGKILL or a crash), so there are no
# stale lock files to clean up and no PID-reuse false positives.
BRIDGE_LOCK = os.path.expanduser("~/.config/computer-agent/tg_bridge.lock")
_bridge_lock_fd: Optional[int] = None


def _hold_bridge_lock() -> bool:
    """Try to take the single-instance lock. Returns False if another bridge
    holds it (caller should exit silently)."""
    global _bridge_lock_fd
    os.makedirs(os.path.dirname(BRIDGE_LOCK), exist_ok=True)
    fd = os.open(BRIDGE_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another process holds the exclusive lock. Read its pid for the log line
        # via os.pread (does NOT take fd ownership, unlike os.fdopen) and close once.
        who = "?"
        try:
            data = os.pread(fd, 256, 0).decode("utf-8", "ignore").strip()
            who = json.loads(data).get("pid", "?") if data else "?"
        except Exception:
            who = "?"
        os.close(fd)
        print(f"Another telegram_bridge is already running (pid {who}); not starting a 2nd.")
        return False
    _bridge_lock_fd = fd  # keep open for the process lifetime → lock held
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps({"pid": os.getpid(), "ts": time.time()}).encode(), 0)
    return True


# ── Shared state between the asyncio loop and the worker thread ─────────────

class Session:
    """Holds the state of one running task and the bridge to the worker thread."""

    def __init__(self, app: Application, loop: asyncio.AbstractEventLoop, chat_id: int):
        self.app = app
        self.loop = loop
        self.chat_id = chat_id

        self.thread: Optional[threading.Thread] = None
        self.running = False
        self.stop_flag = False
        self.last_progress = "idle"

        # For confirm/ask_user: worker thread blocks on this until chat replies.
        self._answer_event = threading.Event()
        self._answer_text: Optional[str] = None
        self._pending_kind: Optional[str] = None   # "confirm" | "ask" | None

        # Live-interrupt mailbox: plain text sent mid-task (not answering a
        # confirm/ask) lands here; the agent drains it before each step.
        self._interrupts: list[str] = []
        self._interrupt_lock = threading.Lock()

    # -- send helpers (called from the worker thread) ----------------------
    def _send(self, text: str) -> None:
        """Thread-safe: schedule a message send on the asyncio loop."""
        asyncio.run_coroutine_threadsafe(
            self.app.bot.send_message(chat_id=self.chat_id, text=text),
            self.loop,
        )

    def _send_photo(self, path: str) -> None:
        async def _go():
            try:
                with open(path, "rb") as f:
                    await self.app.bot.send_photo(chat_id=self.chat_id, photo=f)
            except Exception as e:
                await self.app.bot.send_message(chat_id=self.chat_id, text=f"(screenshot failed: {e})")
        asyncio.run_coroutine_threadsafe(_go(), self.loop)

    def _send_document(self, path: str) -> None:
        async def _go():
            try:
                with open(path, "rb") as f:
                    await self.app.bot.send_document(chat_id=self.chat_id, document=f)
            except Exception as e:
                await self.app.bot.send_message(chat_id=self.chat_id, text=f"(file failed: {e})")
        asyncio.run_coroutine_threadsafe(_go(), self.loop)

    # -- blocking question/confirm (called from worker thread) -------------
    def _ask_blocking(self, kind: str, question: str) -> str:
        """Send a question to chat and block the worker thread until a reply."""
        self._pending_kind = kind
        self._answer_text = None
        self._answer_event.clear()
        if kind == "confirm":
            self._send(f"⚠️ {question}\nReply 'yes' to proceed, or 'no' to cancel.")
        else:
            self._send(f"❓ {question}")
        # Block (up to 10 min) until the user replies via chat.
        self._answer_event.wait(timeout=600)
        self._pending_kind = None
        return self._answer_text or ""

    def deliver_answer(self, text: str) -> bool:
        """Called from the asyncio side when the user replies. Returns True if a
        question was pending and got this answer."""
        if self._pending_kind is None:
            return False
        self._answer_text = text
        self._answer_event.set()
        return True

    # -- live-interrupt mailbox -------------------------------------------
    def add_interrupt(self, text: str) -> None:
        """Queue mid-task guidance (called from the asyncio side)."""
        with self._interrupt_lock:
            self._interrupts.append(text)

    def drain_interrupts(self) -> Optional[str]:
        """Pull & clear all queued guidance (called from the worker thread)."""
        with self._interrupt_lock:
            if not self._interrupts:
                return None
            combined = "\n".join(self._interrupts)
            self._interrupts.clear()
            return combined

    def has_pending_question(self) -> bool:
        return self._pending_kind is not None

    # -- build the hooks the agent loop will call --------------------------
    def make_hooks(self) -> "agent_core.AgentHooks":
        def on_progress(text: str) -> None:
            self.last_progress = text
            self._send(text)

        def confirm(question: str) -> bool:
            ans = self._ask_blocking("confirm", question).strip().lower()
            return ans in ("yes", "y", "نعم", "اوك", "ok")

        def ask_user(question: str) -> str:
            return self._ask_blocking("ask", question)

        def on_screenshot(path: str) -> None:
            self._send_photo(path)

        def should_stop() -> bool:
            return self.stop_flag

        def poll_interrupt() -> Optional[str]:
            return self.drain_interrupts()

        return agent_core.AgentHooks(
            on_progress=on_progress,
            confirm=confirm,
            ask_user=ask_user,
            on_screenshot=on_screenshot,
            should_stop=should_stop,
            poll_interrupt=poll_interrupt,
        )

    # -- run a task in a background thread ---------------------------------
    def start_task(self, task: str) -> None:
        self.running = True
        self.stop_flag = False
        hooks = self.make_hooks()

        def _worker():
            try:
                agent_core.run(task, hooks=hooks)
            except Exception as e:
                self._send(f"⛔ Agent crashed: {e}")
            finally:
                self.running = False
                # ALWAYS free the agent session lock when a task ends, even if
                # run() raised before its own cleanup. The lock is keyed on THIS
                # process's pid (the bridge), which stays alive between tasks — so
                # a crashed task would otherwise leak the lock and make every
                # future /task fail with "another agent instance running" until a
                # restart. _release_lock is a no-op if we don't currently hold it.
                try:
                    agent_core._release_lock()
                except Exception:
                    pass

        self.thread = threading.Thread(target=_worker, daemon=True)
        self.thread.start()


# Single global session (one Mac, one user).
_session: Optional[Session] = None


# ── Auth guard ──────────────────────────────────────────────────────────────

def _authorized(update: Update) -> bool:
    return bool(ALLOWED_CHAT_ID) and str(update.effective_chat.id) == ALLOWED_CHAT_ID


async def _deny(update: Update) -> None:
    if not ALLOWED_CHAT_ID:
        await update.message.reply_text(
            "⛔ Control commands are disabled until TELEGRAM_ALLOWED_CHAT_ID is configured. "
            "Use /whoami to get this chat id."
        )
        return
    await update.message.reply_text("⛔ Not authorized.")


# ── Command handlers ────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    await update.message.reply_text(
        "🤖 Mac agent ready.\n\n"
        "/task <what to do> — run a task\n"
        "/status — current activity\n"
        "/stop — abort\n"
        "/screen — screenshot now\n"
        "/send <path> — send me a file\n"
        "lesson: <rule> — teach a rule I'll remember forever\n"
        "(plain text) — answer a question, or guide a running task\n"
        "/whoami — show this chat id"
    )


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"This chat id is: {cid}\n\n"
        f"Put it in .env as TELEGRAM_ALLOWED_CHAT_ID to lock the bot to you."
    )


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    global _session
    task = " ".join(ctx.args).strip()
    if not task:
        return await update.message.reply_text("Usage: /task open Safari and search for…")
    if _session and _session.running:
        return await update.message.reply_text("⏳ A task is already running. /stop it first.")

    _session = Session(ctx.application, asyncio.get_running_loop(), update.effective_chat.id)
    await update.message.reply_text(f"▶️ Starting: {task}")
    await ctx.application.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    _session.start_task(task)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    if _session and _session.running:
        await update.message.reply_text(f"🔄 {_session.last_progress}")
    else:
        await update.message.reply_text("💤 Idle — no task running.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    if _session and _session.running:
        _session.stop_flag = True
        await update.message.reply_text("🛑 Stopping after the current step…")
    else:
        await update.message.reply_text("Nothing to stop.")


async def cmd_screen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    import subprocess
    path = "/tmp/_agent_manual_screen.png"
    try:
        subprocess.run(["screencapture", "-x", "-t", "png", path], capture_output=True, timeout=10)
        with open(path, "rb") as f:
            await update.message.reply_photo(photo=f)
    except Exception as e:
        await update.message.reply_text(f"Screenshot failed: {e}")


async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorized(update):
        return await _deny(update)
    path = " ".join(ctx.args).strip()
    path = os.path.expanduser(path)
    if not path or not os.path.isfile(path):
        return await update.message.reply_text("Usage: /send /full/path/to/file  (file must exist)")
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(document=f)
    except Exception as e:
        await update.message.reply_text(f"Send failed: {e}")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Plain text: answer a pending confirm/ask, else queue it as live guidance.
    A message starting with 'lesson:' (or 'درس:') teaches a persistent rule."""
    if not _authorized(update):
        return await _deny(update)
    text = (update.message.text or "").strip()
    low = text.lower()
    # "lesson: <rule>" → save a durable, cross-task lesson (and apply it live if a
    # task is running). Checked first so it's never swallowed as an interrupt/answer.
    if low.startswith("lesson:") or low.startswith("درس:"):
        body = text.split(":", 1)[1].strip()
        if not body:
            return await update.message.reply_text("Usage: lesson: <rule in your words>")
        added = lessons_store.add(body, source="user")
        msg = "💾 saved lesson" + ("" if added else " (already known)") + f": {body}"
        if _session and _session.running:
            _session.add_interrupt(f"NEW LESSON (apply this now): {body}")
            msg += " — applying it to the running task too."
        await update.message.reply_text(msg)
        return
    # 1) If the agent is waiting on a confirm/ask, this reply answers it.
    if _session and _session.deliver_answer(text):
        await update.message.reply_text("👍 got it")
    # 2) Else, if a task is running, queue it as mid-task guidance (interrupt).
    elif _session and _session.running:
        _session.add_interrupt(text)
        await update.message.reply_text("📨 noted, I'll factor that in.")
    # 3) Idle: treat a plain-text message as a DIRECT COMMAND and run it — so
    #    "send it anyway" executes a task instead of replying "I'm idle".
    elif _session is not None:
        await update.message.reply_text(f"▶️ Running: {text}")
        _session.start_task(text)
    else:
        await update.message.reply_text("Bridge not ready — send /start.")


# ── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    if "--whoami" in sys.argv:
        print("Start the bot, message it, then use /whoami in the chat.")
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is empty in .env.")
        print("Create a bot with @BotFather, paste the token into .env, and retry.")
        sys.exit(1)

    if not _hold_bridge_lock():
        sys.exit(0)  # another instance is already polling; don't create a 409
    atexit.register(lambda: os.close(_bridge_lock_fd) if _bridge_lock_fd is not None else None)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("send", cmd_send))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    who = ALLOWED_CHAT_ID or "(setup only — control commands disabled; /whoami available)"
    print("=" * 60)
    print("  TELEGRAM BRIDGE — Mac agent remote control")
    print(f"  Allowed chat: {who}")
    print("  Send /start to your bot to begin.")
    print("=" * 60)
    # drop_pending_updates: on (re)start, ignore any backlog of commands that
    # piled up while the bridge was down — otherwise a restart would replay stale
    # /task messages and auto-run them unexpectedly.
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
