from __future__ import annotations

import asyncio

from app.browser import BrowserManager


class FakeLocator:
    def __init__(self, visible: bool = True):
        self.visible = visible
        self.filled = None
        self.clicked = False

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.visible else 0

    async def is_visible(self):
        return self.visible

    async def fill(self, value, timeout=None):
        self.filled = value

    async def click(self, timeout=None):
        self.clicked = True


class FakePage:
    def __init__(self):
        self.otp = FakeLocator()
        self.button = FakeLocator()
        self.keys = []

    def locator(self, selector):
        if "submit" in selector:
            return self.button
        return self.otp

    class Keyboard:
        def __init__(self, page):
            self.page = page

        async def press(self, key):
            self.page.keys.append(key)

    @property
    def keyboard(self):
        return self.Keyboard(self)


def test_totp_field_is_filled_without_exposing_code(settings):
    settings.browser_cors_mode = "off"
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]
    page = FakePage()
    assert asyncio.run(
        manager._submit_totp(page, "JBSWY3DPEHPK3PXP")
    ) is True
    assert page.otp.filled is not None
    assert len(page.otp.filled) == 6
    assert page.button.clicked is True
