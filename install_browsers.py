# install_browsers.py - safe, idempotent for Render native Python
import os
import sys
import subprocess
import time
from pathlib import Path

LOG = lambda *a, **k: print("[install_browsers]", *a, **k)

CANDIDATE_ROOTS = [
    Path("/opt/render/.cache/ms-playwright"),
    Path.home() / ".cache" / "ms-playwright",
    Path("/tmp/.cache/ms-playwright"),
]

def browsers_present():
    for root in CANDIDATE_ROOTS:
        if root.exists():
            # quick probe for headless shell or chrome folder
            for p in root.rglob("*chrome-headless-shell*"):
                if p.exists():
                    LOG("Found existing browser candidate at:", p)
                    return True
            for p in root.rglob("*chromium*"):
                # catch generic chromium folders too
                if p.exists():
                    LOG("Found existing chromium candidate at:", p)
                    return True
    # also check PATH for known executables
    for exe in ("chrome-headless-shell", "chromium", "chrome"):
        if shutil_which(exe):
            LOG(f"Found {exe} in PATH")
            return True
    return False

def shutil_which(name):
    from shutil import which
    return which(name)

def run_install_once():
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    LOG("Running:", " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        LOG("exit_code:", p.returncode)
        if p.stdout:
            LOG("stdout (truncated):", p.stdout.strip()[:2000])
        if p.stderr:
            LOG("stderr (truncated):", p.stderr.strip()[:2000])
        return p.returncode == 0
    except FileNotFoundError:
        LOG("`playwright` command not found. Did you add `playwright` to requirements.txt?")
        return False
    except Exception as e:
        LOG("Exception during install:", repr(e))
        return False

def main():
    LOG("PLAYWRIGHT_BROWSERS_PATH (before):", os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
        LOG("Set PLAYWRIGHT_BROWSERS_PATH=0")

    if browsers_present():
        LOG("Browsers already present — skipping install.")
        return 0

    LOG("Browsers not found — attempting install (no --with-deps)")
    ok = run_install_once()
    if not ok:
        LOG("First install attempt failed, retrying once after 2s...")
        time.sleep(2)
        ok = run_install_once()

    if ok:
        LOG("Playwright Chromium installed (or found).")
    else:
        LOG("Playwright Chromium install failed. Continuing without fatal error — inspect logs.")
    return 0  # keep non-fatal so the service still starts and logs are visible

if __name__ == "__main__":
    sys.exit(main())