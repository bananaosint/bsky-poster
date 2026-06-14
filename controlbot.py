"""
controlbot.py — Telegram Bot Controller
========================================
Supervisor that manages filterbot.py and posterbot.py as child processes.
Controlled from your phone via Telegram bot commands.

Usage:
    python controlbot.py

Required env vars in private.env:
    CONTROL_BOT_TOKEN   — BotFather token for this controller bot
    ADMIN_TELEGRAM_ID   — Your numeric Telegram user ID (only you can issue commands)
"""

import asyncio
import sys
import os
import subprocess
import time
import signal
import logging
from collections import deque
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ==========================================
# [TAG: LOAD DOTENV]
# ==========================================
load_dotenv("private.env")

CONTROL_BOT_TOKEN = os.getenv("CONTROL_BOT_TOKEN")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_TELEGRAM_ID")

if not CONTROL_BOT_TOKEN:
    print("❌ CONTROL_BOT_TOKEN not set in private.env. Get one from @BotFather.")
    sys.exit(1)
if not ADMIN_TELEGRAM_ID:
    print("❌ ADMIN_TELEGRAM_ID not set in private.env. Message @userinfobot to get your numeric ID.")
    sys.exit(1)

ADMIN_TELEGRAM_ID = int(ADMIN_TELEGRAM_ID)

# ==========================================
# [TAG: LOGGING]
# ==========================================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Silence noisy HTTP polling logs from python-telegram-bot internals
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)

# ==========================================
# [TAG: BOT REGISTRY]
# Maps bot names to their script paths.
# Add more bots here if needed.
# ==========================================
BOT_SCRIPTS = {
    "filterbot": "filterbot.py",
    "posterbot": "posterbot.py",
}

# ==========================================
# [TAG: PROCESS MANAGER]
# Tracks child processes + rolling log buffers.
# ==========================================
LOG_BUFFER_SIZE = 200  # lines per bot

class BotProcess:
    """Wraps a child subprocess with metadata and log capture."""

    def __init__(self, name: str, script: str):
        self.name = name
        self.script = script
        self.process: asyncio.subprocess.Process | None = None
        self.logs: deque[str] = deque(maxlen=LOG_BUFFER_SIZE)
        self.started_at: float | None = None
        self._reader_tasks: list[asyncio.Task] = []
        self.saved_heartbeat: str | None = None  # Persists across restarts

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def uptime_str(self) -> str:
        if not self.started_at:
            return "n/a"
        delta = timedelta(seconds=int(time.time() - self.started_at))
        return str(delta)

    async def start(self) -> str:
        if self.running:
            return f"⚠️ **{self.name}** is already running."

        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.script)
        if not os.path.isfile(script_path):
            return f"❌ Script not found: `{script_path}`"

        # Save the last heartbeat before clearing logs so /stats survives restarts
        heartbeat = self._find_heartbeat_in_logs()
        if heartbeat:
            self.saved_heartbeat = heartbeat

        self.logs.clear()
        self._log(f"--- Starting {self.name} ---")

        try:
            # On Windows, CREATE_NEW_PROCESS_GROUP isolates the child so
            # that terminating it never sends signals to the controlbot.
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                **kwargs,
            )
            self.started_at = time.time()

            # Background task to read output line-by-line
            task = asyncio.create_task(self._read_output())
            self._reader_tasks.append(task)

            logger.info(f"Started {self.name} (PID {self.process.pid})")
            return f"✅ **{self.name}** started (PID `{self.process.pid}`)."

        except Exception as e:
            self._log(f"Failed to start: {e}")
            logger.error(f"Failed to start {self.name}: {e}")
            return f"❌ Failed to start **{self.name}**: `{e}`"

    async def stop(self) -> str:
        if not self.running:
            return f"⚠️ **{self.name}** is not running."

        pid = self.process.pid
        self._log(f"--- Stopping {self.name} (PID {pid}) ---")

        # Graceful shutdown: terminate the child process.
        # The child runs in its own process group (CREATE_NEW_PROCESS_GROUP
        # on Windows), so this only affects the child — never the controlbot.
        try:
            self.process.terminate()
        except ProcessLookupError:
            self._log("Process already exited.")
            return f"⚠️ **{self.name}** already exited."

        # Wait up to 10 seconds for graceful exit
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
            self._log(f"Exited gracefully (code {self.process.returncode}).")
        except asyncio.TimeoutError:
            self._log("Grace period expired. Force killing...")
            self.process.kill()
            await self.process.wait()
            self._log(f"Force killed (code {self.process.returncode}).")

        # Cancel reader tasks
        for task in self._reader_tasks:
            task.cancel()
        self._reader_tasks.clear()

        logger.info(f"Stopped {self.name} (PID {pid})")
        return f"🛑 **{self.name}** stopped."

    async def _read_output(self):
        """Continuously reads stdout/stderr and stores in the ring buffer."""
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                print(f"[{self.name}] {decoded}")
                self._log(decoded)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._log(f"[reader error] {e}")

        # If we get here, the process exited
        if self.process.returncode is not None:
            self._log(f"--- {self.name} exited (code {self.process.returncode}) ---")
            logger.warning(f"{self.name} exited with code {self.process.returncode}")

    def _log(self, line: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {line}")

    def get_logs(self, n: int = 50) -> str:
        lines = list(self.logs)[-n:]
        if not lines:
            return f"📭 No logs for **{self.name}** yet."
        text = "\n".join(lines)
        # Telegram message limit is 4096 chars
        if len(text) > 3900:
            text = text[-3900:]
            text = "...(truncated)\n" + text[text.index("\n") + 1:]
        return text

    def _find_heartbeat_in_logs(self) -> str | None:
        """Searches the current log buffer for the most recent heartbeat."""
        for line in reversed(self.logs):
            if "💓" in line:
                return line
        return None

    def get_last_heartbeat(self) -> str | None:
        """Returns the most recent heartbeat — current session first, then saved."""
        live = self._find_heartbeat_in_logs()
        if live:
            return live
        return self.saved_heartbeat

    def clear_saved_heartbeat(self):
        """Clears the saved heartbeat from previous sessions."""
        self.saved_heartbeat = None


# Initialize bot process wrappers
bots: dict[str, BotProcess] = {
    name: BotProcess(name, script)
    for name, script in BOT_SCRIPTS.items()
}

# ==========================================
# [TAG: AUTH DECORATOR]
# Restricts all commands to the admin user.
# ==========================================
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_TELEGRAM_ID:
            logger.warning(
                f"Unauthorized access attempt by {update.effective_user.id} "
                f"({update.effective_user.username})"
            )
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await func(update, context)
    return wrapper


# ==========================================
# [TAG: HELPER — PARSE BOT NAME]
# ==========================================
def parse_bot_name(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    if context.args and context.args[0].lower() in bots:
        return context.args[0].lower()
    return None


def bot_names_list() -> str:
    return ", ".join(f"`{name}`" for name in bots)


# ==========================================
# [TAG: TELEGRAM COMMAND HANDLERS]
# ==========================================

@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **Control Bot — Commands**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Available bots: {bot_names_list()}\n\n"
        "📋 `/status` — Show all bots' status\n"
        "▶️ `/start <bot>` — Start a bot\n"
        "⏹️ `/stop <bot>` — Stop a bot\n"
        "🔄 `/restart <bot>` — Restart a bot\n"
        "🚀 `/startall` — Start all bots\n"
        "🛑 `/stopall` — Stop all bots\n"
        "♻️ `/restartall` — Restart all bots\n"
        "📜 `/logs <bot>` — Last 50 log lines\n"
        "📊 `/stats` — Latest heartbeat from each bot\n"
        "🧹 `/clearstats` — Reset saved heartbeats\n"
        "❓ `/help` — This message\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📡 **Bot Status**\n━━━━━━━━━━━━━━━━"]
    for name, bot in bots.items():
        if bot.running:
            lines.append(
                f"✅ **{name}** — Running (PID `{bot.process.pid}`, uptime {bot.uptime_str})"
            )
        else:
            lines.append(f"⏹️ **{name}** — Stopped")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def cmd_start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = parse_bot_name(context)
    if not name:
        await update.message.reply_text(
            f"Usage: `/start <bot>`\nAvailable: {bot_names_list()}",
            parse_mode="Markdown",
        )
        return
    result = await bots[name].start()
    await update.message.reply_text(result, parse_mode="Markdown")


@admin_only
async def cmd_stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = parse_bot_name(context)
    if not name:
        await update.message.reply_text(
            f"Usage: `/stop <bot>`\nAvailable: {bot_names_list()}",
            parse_mode="Markdown",
        )
        return
    result = await bots[name].stop()
    await update.message.reply_text(result, parse_mode="Markdown")


@admin_only
async def cmd_restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = parse_bot_name(context)
    if not name:
        await update.message.reply_text(
            f"Usage: `/restart <bot>`\nAvailable: {bot_names_list()}",
            parse_mode="Markdown",
        )
        return
    stop_result = await bots[name].stop()
    await update.message.reply_text(stop_result, parse_mode="Markdown")
    await asyncio.sleep(2)  # Brief pause between stop and start
    start_result = await bots[name].start()
    await update.message.reply_text(start_result, parse_mode="Markdown")


@admin_only
async def cmd_startall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    results = []
    for name, bot in bots.items():
        result = await bot.start()
        results.append(result)
    await update.message.reply_text("\n".join(results), parse_mode="Markdown")


@admin_only
async def cmd_stopall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    results = []
    for name, bot in bots.items():
        result = await bot.stop()
        results.append(result)
    await update.message.reply_text("\n".join(results), parse_mode="Markdown")


@admin_only
async def cmd_restartall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Stop all first
    stop_results = []
    for name, bot in bots.items():
        result = await bot.stop()
        stop_results.append(result)
    await update.message.reply_text("\n".join(stop_results), parse_mode="Markdown")

    await asyncio.sleep(2)  # Brief pause between stop and start

    # Start all
    start_results = []
    for name, bot in bots.items():
        result = await bot.start()
        start_results.append(result)
    await update.message.reply_text("\n".join(start_results), parse_mode="Markdown")


@admin_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = parse_bot_name(context)
    if not name:
        await update.message.reply_text(
            f"Usage: `/logs <bot>`\nAvailable: {bot_names_list()}",
            parse_mode="Markdown",
        )
        return
    log_text = bots[name].get_logs(50)
    header = f"📜 **{name} — Last 50 lines**\n━━━━━━━━━━━━━━━━\n"
    await update.message.reply_text(
        header + f"```\n{log_text}\n```",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["📊 **Latest Heartbeats**\n━━━━━━━━━━━━━━━━"]
    for name, bot in bots.items():
        heartbeat = bot.get_last_heartbeat()
        is_saved = heartbeat and heartbeat == bot.saved_heartbeat and not bot._find_heartbeat_in_logs()
        if heartbeat:
            tag = " _(saved)_" if is_saved else ""
            lines.append(f"**{name}:**{tag}\n`{heartbeat}`\n")
        elif bot.running:
            lines.append(f"**{name}:** Running, but no heartbeat yet.\n")
        else:
            lines.append(f"**{name}:** Stopped.\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def cmd_clearstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for name, bot in bots.items():
        bot.clear_saved_heartbeat()
    await update.message.reply_text("🧹 Saved heartbeats cleared.", parse_mode="Markdown")


# ==========================================
# [TAG: GRACEFUL SHUTDOWN]
# When controlbot exits, it stops all child bots.
# ==========================================
async def shutdown_all():
    logger.info("Shutting down all managed bots...")
    for name, bot in bots.items():
        if bot.running:
            logger.info(f"Stopping {name}...")
            await bot.stop()
    logger.info("All bots stopped. Controlbot exiting.")


# ==========================================
# [TAG: MAIN]
# ==========================================
def main():
    logger.info("🤖 Starting Control Bot...")
    logger.info(f"Admin user ID: {ADMIN_TELEGRAM_ID}")
    logger.info(f"Managed bots: {', '.join(BOT_SCRIPTS.keys())}")

    app = ApplicationBuilder().token(CONTROL_BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("start", cmd_start_bot))
    app.add_handler(CommandHandler("stop", cmd_stop_bot))
    app.add_handler(CommandHandler("restart", cmd_restart_bot))
    app.add_handler(CommandHandler("startall", cmd_startall))
    app.add_handler(CommandHandler("stopall", cmd_stopall))
    app.add_handler(CommandHandler("restartall", cmd_restartall))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("clearstats", cmd_clearstats))

    logger.info("✅ Control Bot is running! Send /help to your bot on Telegram.")

    try:
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        pass
    finally:
        # run shutdown coroutine
        import asyncio
        asyncio.run(shutdown_all())


if __name__ == "__main__":
    main()
