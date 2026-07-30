from __future__ import annotations

from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from ..models import BrowserConfig, BrowserMode


class BrowserSession:
    def __init__(self, config: BrowserConfig):
        self.config = config
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.owns_context = False

    async def start(self) -> BrowserContext:
        self.playwright = await async_playwright().start()
        chromium = self.playwright.chromium
        if self.config.mode is BrowserMode.CDP:
            self.browser = await chromium.connect_over_cdp(
                self.config.cdp_url,
                slow_mo=self.config.slow_mo_ms,
                timeout=self.config.navigation_timeout_ms,
            )
            if not self.browser.contexts:
                raise RuntimeError("CDP 浏览器没有可用 context")
            self.context = self.browser.contexts[0]
        else:
            profile = Path(self.config.user_data_dir).resolve()
            profile.mkdir(parents=True, exist_ok=True)
            self.context = await chromium.launch_persistent_context(
                str(profile),
                channel=self.config.channel,
                headless=self.config.headless,
                slow_mo=self.config.slow_mo_ms,
            )
            self.context.set_default_timeout(self.config.navigation_timeout_ms)
            self.owns_context = True
        return self.context

    async def close(self) -> None:
        if self.owns_context and self.context:
            await self.context.close()
        # CDP 模式只断开 Playwright，不关闭用户启动的浏览器。
        elif self.browser:
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
