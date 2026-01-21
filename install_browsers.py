# install_browsers.py - Run this during build to pre-download Playwright browsers
import os
from playwright.sync_api import sync_playwright

def main():
    print("Starting Playwright browser download...")
    with sync_playwright() as p:
        print("Downloading Chromium...")
        p.chromium.download_browser_if_needed()
        print("Chromium download complete!")
        # Optional: also download firefox/webkit if you ever need them
        # p.firefox.download_browser_if_needed()
        # p.webkit.download_browser_if_needed()

if __name__ == "__main__":
    main()