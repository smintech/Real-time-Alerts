# install_browsers.py - Updated for current Playwright (2026)
import os
import subprocess
import sys

def main():
    print("Starting Playwright browser download via CLI...")
    try:
        # Use the Playwright CLI to install only Chromium
        cmd = [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("Chromium installed successfully!")
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print("Failed to install Chromium")
        print(e.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()