from fastapi import FastAPI
from bot.bot import run_bot  # blocking polling function
import multiprocessing
import threading
import uvicorn
import logging
import time
import os
import signal
import sys
import traceback

app = FastAPI()

# -----------------------
# Basic endpoints
# -----------------------
@app.get("/")
def home():
    return {"status": "API Online"}

@app.get("/health")
def health():
    """
    Simple health endpoint:
      - returns OK if process running,
      - returns details otherwise
    """
    return {"status": "ok", "pid": os.getpid()}

# -----------------------
# Logging configuration
# -----------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("app")

# -----------------------
# Bot process manager
# -----------------------
class BotManager:
    def __init__(self, target_callable, max_restarts=5, restart_window_seconds=300):
        """
        Manages a blocking bot function running inside a child process.

        - target_callable: the function to run in child process (run_bot)
        - max_restarts: max restarts allowed within restart_window_seconds
        - restart_window_seconds: sliding window to limit restart storms
        """
        self.target = target_callable
        self.proc = None
        self._lock = threading.Lock()
        self.should_stop = threading.Event()

        # restart policy
        self.max_restarts = max_restarts
        self.restart_window = restart_window_seconds
        self.restart_timestamps = []  # times of recent restarts

    def start(self):
        """Start the bot process once (non-daemon so we can control it)."""
        with self._lock:
            if self.proc and self.proc.is_alive():
                logger.info("Bot process already running (pid=%s)", getattr(self.proc, "pid", None))
                return

            logger.info("Starting bot process...")
            # Use a concrete target wrapper to avoid child import surprises
            self.proc = multiprocessing.Process(target=self._run_target_wrapper)
            self.proc.start()
            logger.info("Started bot process (pid=%s)", self.proc.pid)

    def _run_target_wrapper(self):
        """
        Wrapper executed in subprocess to run user's blocking bot function.
        Any exception is caught and printed — the parent will see process exit code.
        """
        try:
            # Optionally set process title or extra logging here
            self.target()
        except Exception:
            # print stack trace inside child (helpful for debugging when logging is aggregated)
            traceback.print_exc()
            # Ensure child exits with non-zero code
            os._exit(1)
        # clean exit
        os._exit(0)

    def stop(self, timeout=5):
        """Request bot process stop."""
        with self._lock:
            if not self.proc:
                return
            if not self.proc.is_alive():
                logger.info("Bot process not alive; nothing to stop.")
                return

            logger.info("Terminating bot process (pid=%s)...", self.proc.pid)
            try:
                self.proc.terminate()
                self.proc.join(timeout)
                if self.proc.is_alive():
                    logger.warning("Bot did not exit gracefully; killing...")
                    self.proc.kill()
                    self.proc.join(1)
            except Exception as e:
                logger.exception("Error terminating bot process: %s", e)
            finally:
                logger.info("Bot process stopped.")
                self.proc = None

    def monitor_and_restart(self, poll_interval=2):
        """
        Monitor thread. Restarts bot when it dies, according to restart policy.
        This runs in a separate daemon thread in the parent process.
        """
        logger.info("Bot monitor thread started")
        while not self.should_stop.is_set():
            with self._lock:
                proc = self.proc

            if proc is None:
                # not started yet — start it
                try:
                    self.start()
                except Exception:
                    logger.exception("Failed to start bot process")
                    time.sleep(poll_interval)
                    continue
                time.sleep(poll_interval)
                continue

            if not proc.is_alive():
                exitcode = proc.exitcode
                logger.error("Bot process exited (pid=%s, code=%s)", getattr(proc, "pid", None), exitcode)

                # Enforce restart policy: do not restart more than max_restarts within restart_window
                now = time.time()
                # remove old timestamps out of the restart window
                self.restart_timestamps = [t for t in self.restart_timestamps if now - t <= self.restart_window]

                if len(self.restart_timestamps) >= self.max_restarts:
                    logger.critical(
                        "Bot has restarted %s times within %s seconds — halting further restarts to avoid crash loop.",
                        len(self.restart_timestamps), self.restart_window
                    )
                    # mark should_stop so monitor exits and leaves uvicorn running (or caller can stop)
                    self.should_stop.set()
                    break

                # record restart and restart with exponential backoff
                self.restart_timestamps.append(now)
                backoff = min(2 ** len(self.restart_timestamps), 30)
                logger.info("Restarting bot process in %s seconds (attempt #%s)...", backoff, len(self.restart_timestamps))
                time.sleep(backoff)

                # start new process
                try:
                    self.start()
                except Exception:
                    logger.exception("Failed to restart bot process")
            else:
                # healthy; sleep a bit
                time.sleep(poll_interval)

        logger.info("Bot monitor thread exiting")

    def request_stop(self):
        """Signal the monitor to stop and terminate the child process."""
        logger.info("BotManager.request_stop called")
        self.should_stop.set()
        try:
            self.stop()
        except Exception:
            logger.exception("Error while stopping bot on request")

# -----------------------
# Global manager instance (keeps API surface small)
# -----------------------
bot_manager = BotManager(target_callable=run_bot, max_restarts=6, restart_window_seconds=300)

# -----------------------
# Signal handling for graceful shutdown
# -----------------------
def _on_terminate(signum, frame):
    logger.info("Received termination signal (%s). Shutting down...", signum)
    bot_manager.request_stop()
    # give some time for uvicorn to shutdown via normal lifecycle (if running)
    # then exit
    # Note: We don't call sys.exit here because signal handler executes in main thread's context.
    # Let the main thread proceed with shutdown.
signal.signal(signal.SIGINT, _on_terminate)
signal.signal(signal.SIGTERM, _on_terminate)

# -----------------------
# Startup / Shutdown events for FastAPI
# -----------------------
@app.on_event("startup")
def on_startup():
    # Start the bot + monitor thread
    try:
        logger.info("Application startup: starting bot process & monitor")
        bot_manager.start()
        monitor_thread = threading.Thread(target=bot_manager.monitor_and_restart, name="BotMonitor", daemon=True)
        monitor_thread.start()
        # attach monitor thread to manager for possible inspection
        bot_manager._monitor_thread = monitor_thread
    except Exception:
        logger.exception("Error during startup sequence")

@app.on_event("shutdown")
def on_shutdown():
    # Cleanly stop bot process
    logger.info("Application shutdown: stopping bot process")
    bot_manager.request_stop()

# -----------------------
# Main runner (keeps your original pattern but safer)
# -----------------------
if __name__ == "__main__":
    # Optionally configure WORKER_PORT via env (useful on hosting)
    port = int(os.getenv("PORT", "8000"))

    # Brief startup log
    logger.info("Starting FastAPI + bot runner (uvicorn will block). PID=%s", os.getpid())

    # Run Uvicorn programmatically. If Uvicorn crashes, we make sure to stop bot.
    try:
        # Use uvicorn.run directly (blocks current thread). FastAPI lifecycle events will run (startup then shutdown).
        uvicorn.run(app, host="0.0.0.0", port=port, log_level=LOG_LEVEL.lower())
    except Exception as e:
        logger.exception("Uvicorn runtime crashed: %s", e)
    finally:
        # Ensure bot process is stopped before exiting the program
        try:
            logger.info("Main process exiting; stopping bot manager")
            bot_manager.request_stop()
            # small delay to allow cleanup
            time.sleep(0.5)
        except Exception:
            logger.exception("Error while shutting down bot manager")
        logger.info("Exiting main process")