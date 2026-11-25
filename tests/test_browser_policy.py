from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.browser import BrowserManager, validate_url


def test_only_http_and_https():
    for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://example.com"):
        with pytest.raises(ValueError):
            validate_url(url, resolve=False)


def test_blocks_localhost_and_private_networks():
    for url in (
        "http://localhost/",
        "http://app.localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[fd00::1]/",
    ):
        with pytest.raises(ValueError):
            validate_url(url, resolve=False)


def test_blocks_embedded_credentials():
    with pytest.raises(ValueError):
        validate_url("https://user:pass@example.com/", resolve=False)


def test_allowlist_and_subdomains():
    assert (
        validate_url("https://api.github.com/", ["github.com"], resolve=False)
        == "https://api.github.com/"
    )
    with pytest.raises(ValueError):
        validate_url("https://evil.example.net/", ["github.com"], resolve=False)


def test_diagnostics_capture_and_limit(settings):
    settings.browser_cors_mode = "off"
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    class Message:
        type = "error"
        text = "blocked by CORS"
        location = {"url": "https://github.com/"}

    class Request:
        url = "https://api.github.com/x"
        method = "GET"
        failure = "net::ERR_FAILED"

    manager._on_console(Message())
    manager._on_request_failed(Request())
    data = manager.diagnostics()
    assert data["console"][-1]["text"] == "blocked by CORS"
    assert data["failed_requests"][-1]["failure"] == "net::ERR_FAILED"

    for _ in range(150):
        manager._on_console(Message())
    assert len(manager.console_events) <= 120


async def test_multi_step_login_fills_username_then_password(settings):
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    class StepPage:
        url = "https://example.com/login"

        def __init__(self):
            self.fills = []
            self.clicks = []
            self.presses = []
            self.evaluated = 0

        async def fill(self, selector, value, timeout=None):
            self.fills.append((selector, value))

        async def click(self, selector, timeout=None):
            self.clicks.append(selector)

        async def press(self, selector, key):
            self.presses.append((selector, key))

        async def evaluate(self, script):
            self.evaluated += 1
            return {
                "username": "",
                "password": "#password",
                "submit": "#signin",
            }

    page = StepPage()
    filled, otp_submitted, otp_error = await manager._submit_credential(
        page,
        {"username": "me@example.com", "password": "secret", "totp_secret": ""},
        "#username",
        "",
        "#next",
    )

    assert filled == 2
    assert otp_submitted is False
    assert otp_error == ""
    assert page.fills == [
        ("#username", "me@example.com"),
        ("#password", "secret"),
    ]
    assert page.clicks == ["#next", "#signin"]
    assert page.evaluated >= 1


async def test_multi_step_login_password_only(settings):
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    class PasswordPage:
        url = "https://example.com/login/password"

        def __init__(self):
            self.fills = []
            self.presses = []

        async def fill(self, selector, value, timeout=None):
            self.fills.append((selector, value))

        async def press(self, selector, key):
            self.presses.append((selector, key))

    page = PasswordPage()
    filled, otp_submitted, otp_error = await manager._submit_credential(
        page,
        {"username": "me@example.com", "password": "secret", "totp_secret": ""},
        "",
        "#password",
        "",
    )

    assert filled == 1
    assert otp_submitted is False
    assert otp_error == ""
    assert page.fills == [("#password", "secret")]
    assert page.presses == [("#password", "Enter")]


def test_browser_status_exposes_remote_view(settings):
    settings.browser_cdp_url = "http://browser:9223"
    settings.browser_vnc_url = "http://127.0.0.1:6080/vnc.html"
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]
    status = manager.status()
    assert status["remote"] is True
    assert status["view_url"] == "http://127.0.0.1:6080/vnc.html"


def test_view_url_follows_request_host(settings):
    from app.browser import rewrite_view_host

    url = "http://192.168.1.6:6080/vnc.html?autoconnect=1&reconnect=1"
    assert (
        rewrite_view_host(url, "Jetson.local")
        == "http://jetson.local:6080/vnc.html?autoconnect=1&reconnect=1"
    )
    assert (
        rewrite_view_host(url, "10.0.0.5")
        == "http://10.0.0.5:6080/vnc.html?autoconnect=1&reconnect=1"
    )
    # No explicit port in the config -> none in the rewrite.
    assert rewrite_view_host("http://host/vnc.html", "x.test") == "http://x.test/vnc.html"
    # IPv6 literals keep their brackets.
    assert rewrite_view_host("http://[::1]:6080/vnc.html", "[::1]") == "http://[::1]:6080/vnc.html"
    # Unusable input falls back to the configured URL.
    assert rewrite_view_host(url, None) == url
    assert rewrite_view_host(url, "") == url
    assert rewrite_view_host(url, "host/evil") == url
    assert rewrite_view_host("", "x.test") == ""
    assert rewrite_view_host("javascript:alert(1)", "x.test") == "javascript:alert(1)"

    settings.browser_vnc_url = url
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]
    assert (
        manager.status(request_host="jetson.local")["view_url"]
        == "http://jetson.local:6080/vnc.html?autoconnect=1&reconnect=1"
    )
    assert (
        manager.diagnostics(request_host="jetson.local")["view_url"]
        == "http://jetson.local:6080/vnc.html?autoconnect=1&reconnect=1"
    )


def test_agent_contexts_are_isolated(settings):
    settings.browser_enabled = True
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    class FakePage:
        def __init__(self, context, url="about:blank"):
            self.context = context
            self.url = url
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

        def on(self, event, callback):
            return None

        async def bring_to_front(self):
            self.context.focused = getattr(self.context, "focused", 0) + 1

    class FakeContext:
        def __init__(self):
            self.pages = []
            self.routes = []
            self.listeners = []

        async def new_page(self):
            page = FakePage(self)
            self.pages.append(page)
            return page

        def set_default_timeout(self, value):
            return None

        async def route(self, pattern, callback):
            self.routes.append((pattern, callback))

        def on(self, event, callback):
            self.listeners.append((event, callback))

    class FakeBrowser:
        def __init__(self):
            self.contexts = [FakeContext()]
            self.created = []

        async def new_context(self, **options):
            context = FakeContext()
            self.created.append(context)
            return context

    fake_browser = FakeBrowser()
    manager._remote_browser = fake_browser
    manager._context = fake_browser.contexts[0]
    manager._contexts = {"default": manager._context}
    manager._pages["default"] = asyncio.run(manager._context.new_page())
    manager._last_urls["default"] = ""

    assert manager.status("agent:5")["started"] is False
    page = asyncio.run(manager._ensure_page("agent:5"))

    assert page.url == "about:blank"
    assert manager._contexts["default"] is fake_browser.contexts[0]
    assert manager._contexts["agent:5"] is fake_browser.created[0]
    assert manager._contexts["agent:5"] is not manager._contexts["default"]
    assert manager._active_session == "default"
    assert fake_browser.contexts[0].focused == 1
    assert manager.status("agent:5")["started"] is True
    assert manager.status("agent:11")["started"] is False
    assert manager.status("agent:5")["has_window"] is True


def test_browser_status_is_scoped_to_the_requested_window(settings):
    settings.browser_enabled = True
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    class FakePage:
        url = "https://example.com/agent-5"
        closed = False

        def is_closed(self):
            return self.closed

    manager._context = object()
    manager._contexts = {"default": object(), "agent:5": object()}
    manager._pages = {"default": FakePage(), "agent:5": FakePage()}
    manager._last_urls = {"default": "", "agent:5": ""}

    assert manager.status("default")["started"] is True
    assert manager.status("default")["has_window"] is True
    assert manager.status("agent:11")["started"] is False
    assert manager.status("agent:11")["has_window"] is False


def test_cors_injection_is_domain_scoped(settings):
    settings.browser_cors_mode = "inject"
    settings.browser_cors_domains = "github.com,githubusercontent.com"
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    assert manager._cors_domain_match("github.com")
    assert manager._cors_domain_match("api.github.com")
    assert not manager._cors_domain_match("evilgithub.com")
    assert not manager._cors_domain_match("example.com")

    class FakeRoute:
        def __init__(self):
            self.result = None

        async def fulfill(self, **kwargs):
            self.result = kwargs

    route = FakeRoute()
    request = SimpleNamespace(
        url="https://api.github.com/rate_limit",
        method="OPTIONS",
        resource_type="xhr",
        headers={
            "origin": "https://github.com",
            "access-control-request-method": "GET",
            "access-control-request-headers": "content-type",
        },
    )
    assert asyncio.run(manager._inject_cors(route, request)) is True
    headers = route.result["headers"]
    assert headers["access-control-allow-origin"] == "https://github.com"
    assert headers["access-control-allow-credentials"] == "true"

    malicious = SimpleNamespace(
        url="https://api.github.com/rate_limit",
        method="OPTIONS",
        resource_type="xhr",
        headers={"origin": "https://evil.example"},
    )
    assert asyncio.run(manager._inject_cors(FakeRoute(), malicious)) is False

    offsite = SimpleNamespace(
        url="https://example.com/data",
        method="OPTIONS",
        resource_type="xhr",
        headers={"origin": "https://github.com"},
    )
    assert asyncio.run(manager._inject_cors(FakeRoute(), offsite)) is False


def test_otp_detection_scans_child_frames(settings):
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    class Frame:
        def __init__(self, info):
            self.info = info

        async def evaluate(self, script):
            return self.info

    class Page:
        def __init__(self):
            self.url = "https://example.com/login"
            self.frame0 = Frame(None)
            self.main_frame = self.frame0
            self.frames = [
                self.frame0,
                Frame({"otp": "#code", "submit": "#go", "segmented": True}),
            ]

        async def evaluate(self, script):
            return None

        async def title(self):
            return "Sign in"

    page = Page()

    async def ensure(session):
        return page

    manager._ensure_page = ensure  # type: ignore[assignment]
    info = asyncio.run(manager.detect_otp_fields())
    assert info["otp"] == "#code"
    assert info.get("frame") is True
    assert info.get("segmented") is True


def test_read_flags_javascript_only_pages(settings):
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    class Page:
        url = "https://spa.example/"

        async def inner_text(self, selector):
            return ""

        async def title(self):
            return "SPA"

        async def evaluate(self, script):
            return {"scripts": 7, "text_length": 0, "has_root": True}

    async def ensure(session):
        return Page()

    manager._ensure_page = ensure  # type: ignore[assignment]
    data = asyncio.run(manager.read())
    assert data["needs_javascript"] is True
    assert "[needs-JS]" in data["text"]


def test_read_does_not_flag_text_pages(settings):
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    class Page:
        url = "https://example.com/"

        async def inner_text(self, selector):
            return "Plenty of readable text. " * 20

        async def title(self):
            return "Normal"

        async def evaluate(self, script):
            return {"scripts": 2, "text_length": 600, "has_root": False}

    async def ensure(session):
        return Page()

    manager._ensure_page = ensure  # type: ignore[assignment]
    data = asyncio.run(manager.read())
    assert "needs_javascript" not in data


class FakeKeyboard:
    def __init__(self):
        self.pressed = []

    async def press(self, key):
        self.pressed.append(key)


class FakeLocator:
    def __init__(self, count=1, click_errors=None, sink=None, fill_sink=None):
        self._count = count
        self.click_errors = list(click_errors or [])
        self.sink = sink if sink is not None else []
        self.fill_sink = fill_sink if fill_sink is not None else []
        self.fills = []

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return True

    async def scroll_into_view_if_needed(self, timeout=None):
        return None

    async def click(self, timeout=None, force=False):
        self.sink.append(force)
        if self.click_errors:
            error = self.click_errors.pop(0)
            if error:
                raise RuntimeError(error)

    async def fill(self, text, timeout=None):
        self.fills.append(text)
        self.fill_sink.append(text)

    async def press(self, key, timeout=None):
        self.fills.append("<enter>")

    async def dispatch_event(self, name, timeout=None):
        self.sink.append("dispatch")

    def locator(self, selector):
        return FakeLocator(count=0, sink=self.sink, fill_sink=self.fill_sink)

    async def get_attribute(self, name, timeout=None):
        return ""


class FakeFrame:
    def __init__(self, url="", by_text=None, by_value=None, by_label=None):
        self.url = url
        self.by_text = set(by_text or [])
        self.by_value = set(by_value or [])
        self.by_label = set(by_label or [])
        self.body_text = ""
        self.clicked = []
        self.fills = []
        self.fill_errors = []

    def _locator(self, match):
        errors = self.fill_errors if match else []
        return FakeLocator(
            count=1 if match else 0,
            click_errors=errors,
            sink=self.clicked,
            fill_sink=self.fills,
        )

    def locator(self, selector):
        if selector.startswith('input[value="'):
            value = selector.split('"')[1]
            return self._locator(value.lower() in {v.lower() for v in self.by_value})
        return self._locator(False)

    def get_by_text(self, text, exact=False):
        return self._locator(text in self.by_text)

    def get_by_role(self, role, name=None, exact=False):
        return self._locator(False)

    def get_by_label(self, text, exact=False):
        return self._locator(text in self.by_label)

    def get_by_placeholder(self, text, exact=False):
        return self._locator(False)

    async def inner_text(self, selector):
        return self.body_text


class FakePage(FakeFrame):
    def __init__(self, frames=None, **kwargs):
        super().__init__(url="https://example.com/page", **kwargs)
        self.frames = [self] + list(frames or [])
        self.main_frame = self
        self.keyboard = FakeKeyboard()

    async def title(self):
        return "Fake page"

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def evaluate(self, script):
        if "scripts" in str(script):
            return {"scripts": 0, "text_length": 40, "has_root": False}
        return None


def _manager_with_page(settings, page):
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]

    async def ensure(session):
        return page

    manager._ensure_page = ensure  # type: ignore[assignment]
    return manager


def test_click_finds_value_button_inside_iframe(settings):
    frame = FakeFrame(url="https://example.com/modal", by_value={"Confirm"})
    page = FakePage(frames=[frame])
    manager = _manager_with_page(settings, page)
    result = asyncio.run(manager.click("Confirm"))
    assert result["url"] == page.url
    assert frame.clicked  # the click landed inside the iframe


def test_click_retries_with_force_when_overlay_blocks(settings):
    page = FakePage(by_text={"Confirm"})
    page.fill_errors = ["element is not receiving pointer events (intercepted)"]
    manager = _manager_with_page(settings, page)
    asyncio.run(manager.click("Confirm"))
    assert page.clicked[:2] == [False, True]


def test_click_reports_actionable_no_match(settings):
    page = FakePage()
    manager = _manager_with_page(settings, page)
    with pytest.raises(Exception) as info:
        asyncio.run(manager.click("Nope"))
    message = str(info.value)
    assert "No element matched" in message
    assert "kind='css'" in message


def test_type_matches_by_label(settings):
    page = FakePage(by_label={"Email address"})
    manager = _manager_with_page(settings, page)
    asyncio.run(manager.type_text("Email address", "me@example.com"))
    assert page.clicked  # focused via click
    assert "me@example.com" in page.fills


def test_read_includes_iframe_text(settings):
    frame = FakeFrame(url="https://example.com/frame")
    frame.body_text = "The warning dialog says Confirm to continue."
    page = FakePage(frames=[frame])
    page.body_text = "Main page body"
    manager = _manager_with_page(settings, page)
    data = asyncio.run(manager.read())
    assert "Main page body" in data["text"]
    assert "[frame https://example.com/frame]" in data["text"]
    assert "Confirm to continue" in data["text"]


class FakePrefs:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_browser_private_access_follows_pref(settings):
    settings.browser_allow_private_networks = False
    blocked = BrowserManager(settings, None, FakePrefs({}))
    with pytest.raises(Exception) as info:
        asyncio.run(blocked._checked_url("http://10.0.0.5/"))
    message = str(info.value)
    assert "private" in message.lower() or "LAN" in message

    allowed = BrowserManager(
        settings, None, FakePrefs({"browser_allow_private": True})
    )
    assert (
        asyncio.run(allowed._checked_url("http://10.0.0.5/"))
        == "http://10.0.0.5/"
    )


def test_click_warns_when_page_did_not_change(settings):
    page = FakePage(by_text={"Submit"})
    page.body_text = "Login form"
    manager = _manager_with_page(settings, page)
    result = asyncio.run(manager.click("Submit"))
    assert result.get("unchanged") is True
    assert "silently" in result.get("warning", "")


def test_type_submit_warns_when_page_did_not_change(settings):
    page = FakePage(by_label={"User"})
    page.body_text = "Login form"
    manager = _manager_with_page(settings, page)
    result = asyncio.run(manager.type_text("User", "me", submit=True))
    assert result.get("unchanged") is True


def test_private_networks_can_be_enabled_for_dev():
    assert (
        validate_url("http://127.0.0.1:8080/", allow_private=True, resolve=False)
        == "http://127.0.0.1:8080/"
    )


def test_public_host_without_dns_lookup():
    assert validate_url("https://example.com/path", resolve=False) == (
        "https://example.com/path"
    )


def test_rejects_malformed_urls():
    with pytest.raises(ValueError):
        validate_url("", resolve=False)
    with pytest.raises(ValueError):
        validate_url("not a url", resolve=False)
