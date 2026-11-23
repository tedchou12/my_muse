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


def test_browser_status_exposes_remote_view(settings):
    settings.browser_cdp_url = "http://browser:9223"
    settings.browser_vnc_url = "http://127.0.0.1:6080/vnc.html"
    manager = BrowserManager(settings, None)  # type: ignore[arg-type]
    status = manager.status()
    assert status["remote"] is True
    assert status["view_url"] == "http://127.0.0.1:6080/vnc.html"


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
