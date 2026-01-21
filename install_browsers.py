# install_browsers.py - Final version for Render native Python (no deps, pure browser download)
import subprocess
import sys

def main():
    print("Installing Playwright Chromium browser only (no system deps - safe for Render)")
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print("Playwright Chromium installed successfully!")
        print(result.stdout)
    except subprocess.CalledProcessError as e:
        print("Installation failed")
        print(e.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()