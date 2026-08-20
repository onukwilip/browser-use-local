# browser.py
import asyncio
from patchright.async_api import async_playwright, Playwright, Browser
from config import BROWSER_HEADLESS, BROWSER_USER_DATA_DIR
import tempfile
import shutil
import itertools

_port_counter = itertools.count(9222)

class PatchrightBrowser:
    """
    Manages one Patchright Chromium instance per request lifecycle.
    Launched fresh per task, closed after. This avoids state leaking
    between company research sessions.
    """

    def __init__(self):
        self.debug_port = next(_port_counter)
        self._playwright: Playwright = None
        self._browser: Browser = None
        self._temp_dir    = None

    async def start(self) -> str:
        """
        Launches Patchright Chromium with remote debugging enabled.
        Returns the CDP URL BU should connect to.
        """
        self._temp_dir = tempfile.mkdtemp(prefix="bu-session-")
        self._playwright = await async_playwright().start()

        # launch_persistent_context is Patchright's recommended stealth config.
        # channel="chrome" uses the installed Google Chrome binary (more stealth
        # than Chromium). Falls back to Chromium if Chrome is not installed.
        # no_viewport=True is a key stealth patch Patchright applies.
        self._browser = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self._temp_dir,
            channel="chrome",
            headless=BROWSER_HEADLESS,
            no_viewport=True,
            args=[
                f"--remote-debugging-port={self.debug_port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        cdp_url = f"http://localhost:{self.debug_port}"
        # Give Chrome a moment to open the CDP socket
        await asyncio.sleep(1)
        return cdp_url

    async def stop(self):
        """Closes the browser and Playwright instance."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
