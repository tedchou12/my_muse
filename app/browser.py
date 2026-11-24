"""Sandboxed Chromium the agent (and you) can watch and operate.

Each named agent gets its own isolated browser context and Chromium window, so
agents do not share tabs or cookies. The default/Muse context uses the
persistent profile; named-agent contexts are isolated per app session.

Every network request is filtered:

* only http/https
* no URLs with embedded credentials
* localhost, link-local, private and reserved IP ranges are blocked
* optional domain allowlist

The container is the real security boundary: treat anything inside it as
untrusted.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

from .config import Settings
from .totp import TotpError, code as totp_code
from .vault import Vault, host_matches

logger = logging.getLogger("muse.browser")

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}

OTP_CHAT_RE = re.compile(
    r"^\s*(?:(?:my|the)\s+)?"
    r"(?:(?:otp|code|verification\s+code|one[-\s]?time\s+code)\s*"
    r"(?:is|:|=|#)?\s*)?"
    r"([A-Za-z0-9-]{4,20})\s*$",
    re.I,
)


class BrowserError(Exception):
    pass


class BrowserUnavailable(BrowserError):
    pass


def _check_ip(raw: str, allow_private: bool) -> None:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        raise ValueError("Could not parse IP address %r" % raw)
    if allow_private:
        return
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global:
        raise ValueError("Blocked non-public address %s" % ip)


def validate_url(
    url: str,
    allowed_domains: Optional[List[str]] = None,
    allow_private: bool = False,
    resolve: bool = True,
) -> str:
    """Return the URL if it is safe to fetch, otherwise raise ValueError."""
    if not isinstance(url, str):
        raise ValueError("URL must be a string.")
    url = url.strip()
    if not url or len(url) > 2048:
        raise ValueError("URL is empty or too long.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed.")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("URL has no hostname.")
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise ValueError("Blocked hostname %r." % host)

    literal_ip = None
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    if literal_ip is not None:
        _check_ip(str(literal_ip), allow_private)

    if allowed_domains:
        if not any(
            host == domain or host.endswith("." + domain)
            for domain in allowed_domains
        ):
            raise ValueError("Host %r is not in BROWSER_ALLOWED_DOMAINS." % host)

    if resolve and literal_ip is None:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("Could not resolve %r: %s" % (host, exc))
        for info in infos:
            _check_ip(info[4][0], allow_private)
    return parsed.geturl()


def _collapse(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) > limit:
        return text[:limit] + " …[truncated]"
    return text


LOGIN_FIELDS_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && el.getClientRects().length > 0;
  };
  const esc = (value) => (window.CSS && CSS.escape)
    ? CSS.escape(value)
    : String(value).replace(/[^A-Za-z0-9_-]/g, '\\$&');
  const selectorFor = (el) => {
    if (!el) return '';
    if (el.id) return '#' + esc(el.id);
    const name = el.getAttribute('name');
    if (name) {
      const base = el.tagName.toLowerCase() + '[name="' + name.replace(/"/g, '\\"') + '"]';
      try { if (document.querySelectorAll(base).length === 1) return base; } catch (e) {}
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      if (node.id) {
        parts.unshift('#' + esc(node.id));
        break;
      }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  const textInput = (el) => visible(el) && ![
    'hidden', 'password', 'submit', 'button', 'checkbox', 'radio', 'file', 'image', 'reset'
  ].includes((el.type || 'text').toLowerCase());
  const usernameIn = (scope) => {
    const inputs = Array.from(scope.querySelectorAll('input')).filter(textInput);
    return inputs.find((el) => /user|email|login|account|identifier|phone|mail/i.test(
      [el.name, el.id, el.autocomplete, el.placeholder, el.getAttribute('aria-label')].join(' ')
    )) || inputs.find((el) => ['text', 'email', 'tel'].includes((el.type || 'text').toLowerCase()));
  };
  const password = Array.from(document.querySelectorAll('input[type="password"]')).find(visible) || null;
  let username = null;
  let scope = null;
  if (password) {
    scope = password.closest('form') || document;
    username = usernameIn(scope);
  } else {
    username = usernameIn(document);
    if (!username) return null;
    scope = username.closest('form') || document;
  }
  const submitButtons = Array.from(
    scope.querySelectorAll('button, input[type="submit"], [role="button"]')
  ).filter(visible);
  let submit = submitButtons.find((el) =>
    /log ?in|sign ?in|submit|continue|next/i.test(el.innerText || el.value || '')
  );
  if (!submit) submit = submitButtons[0];
  if (!submit) {
    submit = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible).find((el) =>
      /log ?in|sign ?in|submit|continue|next/i.test(el.innerText || '')
    );
  }
  return {
    url: location.href,
    title: document.title,
    username: selectorFor(username),
    password: selectorFor(password),
    submit: selectorFor(submit),
    form_action: (scope && scope.action) || ''
  };
}
"""
OTP_FIELDS_JS = r"""
() => {
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && el.getClientRects().length > 0;
  };
  const esc = (value) => (window.CSS && CSS.escape)
    ? CSS.escape(value)
    : String(value).replace(/[^A-Za-z0-9_-]/g, '\\$&');
  const selectorFor = (el) => {
    if (!el) return '';
    if (el.id) return '#' + esc(el.id);
    const name = el.getAttribute('name');
    if (name) {
      const base = el.tagName.toLowerCase() + '[name="' + name.replace(/"/g, '\\"') + '"]';
      try { if (document.querySelectorAll(base).length === 1) return base; } catch (e) {}
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      if (node.id) {
        parts.unshift('#' + esc(node.id));
        break;
      }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  const selectors = [
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[id*="otp" i]',
    'input[name*="otc" i]',
    'input[name*="verification" i]',
    'input[name*="mfa" i]',
    'input[name*="code" i]',
    'input[id*="code" i]',
    'input[name*="pin" i]',
    'input[id*="pin" i]',
    'input[aria-label*="one-time" i]',
    'input[aria-label*="verification" i]',
    'input[aria-label*="security code" i]',
    'input[aria-label*="auth code" i]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]',
    'input[placeholder*="verification" i]',
    'input[inputmode="numeric"]'
  ];
  const describe = (el) =>
    [
      el.getAttribute('name'),
      el.getAttribute('id'),
      el.getAttribute('aria-label'),
      el.getAttribute('placeholder'),
      el.getAttribute('autocomplete'),
      el.getAttribute('title')
    ]
      .filter(Boolean)
      .join(' ');
  const otpish = (el) => /otp|one.?time|verif|security ?code|auth(?:orization)? ?code|sms ?code|passcode|2fa|mfa|token/i.test(describe(el));
  let otp = null;
  for (const selector of selectors) {
    try {
      otp = Array.from(document.querySelectorAll(selector)).find((el) => {
        if (!visible(el) || el.disabled || el.readOnly) return false;
        if ((el.type || '').toLowerCase() === 'search') return false;
        if (selector === 'input[inputmode="numeric"]') {
          if (!(el.maxLength >= 4 && el.maxLength <= 10)) return false;
          return otpish(el) || /code|verif|passcode/i.test(describe(el));
        }
        return true;
      }) || null;
      if (otp) break;
    } catch (e) {}
  }
  if (!otp) {
    try {
      otp = Array.from(document.querySelectorAll('input')).find((el) => {
        if (!visible(el) || el.disabled || el.readOnly) return false;
        const type = (el.type || '').toLowerCase();
        if (['hidden', 'checkbox', 'radio', 'search', 'submit', 'button', 'file'].includes(type)) return false;
        return otpish(el) && (el.maxLength === -1 || (el.maxLength >= 4 && el.maxLength <= 10));
      }) || null;
    } catch (e) {}
  }
  if (!otp) return null;
  let segmented = false;
  try {
    if (otp.maxLength === 1) {
      const group = Array.from(document.querySelectorAll('input[maxlength="1"]')).filter(visible);
      if (group.length >= 4) segmented = true;
    }
  } catch (e) {}
  const scope = otp.closest('form') || document;
  const buttons = Array.from(
    scope.querySelectorAll('button[type="submit"], input[type="submit"], button:not([type])')
  ).filter(visible);
  let submit = buttons.find((el) =>
    /verify|submit|continue|next|confirm|sign ?in|log ?in/i.test(el.innerText || el.value || '')
  );
  if (!submit) submit = buttons[0];
  return {
    url: location.href,
    title: document.title,
    otp: selectorFor(otp),
    submit: selectorFor(submit),
    segmented: segmented,
    text: (document.body.innerText || '').slice(0, 1200)
  };
}
"""





NEEDS_JS_JS = r"""
() => ({
  scripts: document.querySelectorAll('script').length,
  text_length: document.body ? (document.body.innerText || '').trim().length : 0,
  has_root: Boolean(document.querySelector('#root, #app, #__next, [data-reactroot], [ng-version]'))
})
"""


TARGET_KINDS = ("auto", "css", "text", "label", "placeholder", "role", "value")
ROLE_NAMES = (
    "button",
    "link",
    "textbox",
    "checkbox",
    "radio",
    "combobox",
    "tab",
    "menuitem",
    "option",
    "searchbox",
)


def _css_attr_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def rewrite_view_host(url: str, host: Optional[str]) -> str:
    """Rewrite a configured view URL's hostname with the caller's host.

    The configured BROWSER_VNC_URL supplies the scheme, port and path; the
    hostname is taken from the request's Host header so the live view keeps
    working no matter which hostname or IP the user reached Muse on. Returns
    the configured URL unchanged when there is nothing sensible to swap.
    """
    if not url:
        return url
    host = (host or "").strip().lower()
    if not host or any(ch in host for ch in "/\\?#@ \t\r\n"):
        return url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return url
    candidate = urlparse("//" + host)
    new_host = candidate.hostname
    if not new_host:
        return url
    if ":" in new_host:  # IPv6 literal needs brackets in a netloc
        new_host = "[%s]" % new_host
    netloc = new_host
    if parsed.port is not None:
        netloc += ":%d" % parsed.port
    return urlunparse(parsed._replace(netloc=netloc))


class BrowserManager:
    def __init__(self, settings: Settings, vault: Vault) -> None:
        self.settings = settings
        self.vault = vault
        self._lock: Optional[asyncio.Lock] = None
        self._playwright: Any = None
        self._context: Any = None
        self._contexts: Dict[str, Any] = {}
        self._page: Any = None
        self._remote_browser: Any = None
        self._local_browser: Any = None
        self._remote = False
        self._remote_sw_warned = False
        self._pages: Dict[str, Any] = {}
        self._last_urls: Dict[str, str] = {}
        self._active_session = "default"
        self.last_url = ""
        self.console_events: List[Dict[str, Any]] = []
        self.failed_requests: List[Dict[str, Any]] = []
        self.cors_injections = 0
        self._monitored_pages: set[int] = set()
        self._console_audit_seen: set[tuple[str, str]] = set()
        self._cors_audited: set[str] = set()

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # -- diagnostics ------------------------------------------------------
    def _attach_page(self, page: Any) -> None:
        if id(page) in self._monitored_pages:
            return
        self._monitored_pages.add(id(page))
        page.on("console", self._on_console)
        page.on("requestfailed", self._on_request_failed)

    def _on_console(self, message: Any) -> None:
        if getattr(message, "type", "") not in ("error", "warning"):
            return
        text = (message.text or "")[:2000]
        location = getattr(message, "location", {}) or {}
        self.console_events.append(
            {
                "ts": time.time(),
                "type": message.type,
                "text": text,
                "location": location,
            }
        )
        self.console_events[:] = self.console_events[-120:]
        if message.type == "error" and self.vault is not None:
            url = str(location.get("url", ""))[:500]
            key = (text[:500], url)
            if key not in self._console_audit_seen:
                self._console_audit_seen.add(key)
                if len(self._console_audit_seen) > 200:
                    self._console_audit_seen.clear()
                try:
                    self.vault.audit.log(
                        "browser",
                        "browser_console_error",
                        {"text": text[:500], "url": url},
                    )
                except Exception:
                    logger.debug("Could not audit console error", exc_info=True)

    def _on_request_failed(self, request: Any) -> None:
        self.failed_requests.append(
            {
                "ts": time.time(),
                "url": (request.url or "")[:2000],
                "method": request.method,
                "failure": str(request.failure or "")[:500],
            }
        )
        self.failed_requests[:] = self.failed_requests[-120:]

    def diagnostics(
        self, session: str = "default", request_host: Optional[str] = None
    ) -> Dict[str, Any]:
        session = (session or "default").strip()[:100] or "default"
        return {
            "url": self._last_urls.get(session, self.last_url),
            "session": session,
            "console": list(self.console_events[-120:]),
            "failed_requests": list(self.failed_requests[-120:]),
            "cors_mode": self.settings.browser_cors_mode,
            "cors_domains": self.settings.cors_domains,
            "cors_injections": self.cors_injections,
            "block_service_workers": self.settings.browser_block_service_workers,
            "remote": self._remote or bool(self.settings.browser_cdp_url),
            "view_url": rewrite_view_host(
                self.settings.browser_vnc_url, request_host
            ),
        }

    # -- lifecycle --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.settings.browser_enabled)

    def status(
        self, session: str = "default", request_host: Optional[str] = None
    ) -> Dict[str, Any]:
        session = (session or "default").strip()[:100] or "default"
        page = self._pages.get(session)
        started = bool(page is not None and not page.is_closed())
        sessions = []
        for key, item in self._pages.items():
            if item is None or item.is_closed():
                continue
            sessions.append(
                {
                    "id": key,
                    "url": self._last_urls.get(key) or item.url,
                    "active": key == self._active_session,
                }
            )
        return {
            "enabled": self.enabled,
            "started": started,
            "has_window": bool(self._contexts.get(session) is not None),
            "url": (page.url if page and not page.is_closed() else self._last_urls.get(session, self.last_url)),
            "cors_mode": self.settings.browser_cors_mode,
            "cors_domains": self.settings.cors_domains,
            "block_service_workers": self.settings.browser_block_service_workers,
            "remote": self._remote or bool(self.settings.browser_cdp_url),
            "view_url": rewrite_view_host(
                self.settings.browser_vnc_url, request_host
            ),
            "session": session,
            "active_session": self._active_session,
            "sessions": sessions,
        }

    async def _prepare_context(self, context: Any) -> Any:
        context.set_default_timeout(15_000)
        await context.route("**/*", self._guard_route)
        context.on("page", self._attach_page)
        for existing_page in context.pages:
            self._attach_page(existing_page)
        return context

    async def _new_agent_context(self) -> Any:
        options = {
            "viewport": {"width": 1280, "height": 800},
            "accept_downloads": False,
            "service_workers": (
                "block" if self.settings.browser_block_service_workers else "allow"
            ),
        }
        if self._remote_browser is not None:
            context = await self._remote_browser.new_context(**options)
        elif self._context is not None:
            owner = getattr(self._context, "browser", None)
            if owner is None:
                if self._local_browser is None:
                    self._local_browser = await self._playwright.chromium.launch(
                        headless=bool(self.settings.browser_headless),
                        service_workers=options["service_workers"],
                        args=[
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-extensions",
                            "--no-first-run",
                        ],
                    )
                owner = self._local_browser
            context = await owner.new_context(**options)
        else:
            raise BrowserUnavailable("The browser is not connected.")
        return await self._prepare_context(context)

    async def _context_for(self, session: str) -> Any:
        context = self._contexts.get(session)
        if context is not None:
            return context
        if session == "default" and self._context is not None:
            self._contexts["default"] = self._context
            return self._context
        context = await self._new_agent_context()
        self._contexts[session] = context
        return context

    async def _trim_context_pages(self, context: Any, keep: Any) -> None:
        for item in context.pages:
            if item is keep or item.is_closed():
                continue
            try:
                await item.close()
            except Exception:
                logger.debug("Could not close a stale browser page", exc_info=True)

    async def _ensure_page(self, session: str = "default") -> Any:
        if not self.enabled:
            raise BrowserUnavailable("The browser is disabled (BROWSER_ENABLED=false).")
        session = (session or "default").strip()[:100] or "default"
        existing = self._pages.get(session)
        if existing is not None and not existing.is_closed():
            return existing
        if self._context is None:
            if self.settings.browser_cdp_url:
                return await self._connect_remote_page(session)
            await self._launch_local_browser()
        previous = self._active_session
        previous_page = self._pages.get(previous)
        context = await self._context_for(session)
        page = await context.new_page()
        self._attach_page(page)
        self._pages[session] = page
        self._last_urls[session] = ""
        if (
            previous != session
            and previous_page is not None
            and not previous_page.is_closed()
        ):
            try:
                await previous_page.bring_to_front()
            except Exception:
                pass
        return page

    async def _launch_local_browser(self) -> None:
        from playwright.async_api import async_playwright

        if self._local_browser is not None:
            try:
                await self._local_browser.close()
            except Exception:
                pass
            self._local_browser = None
        self._playwright = await async_playwright().start()
        profile_dir = self.settings.browser_dir / "profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=bool(self.settings.browser_headless),
                service_workers=(
                    "block" if self.settings.browser_block_service_workers else "allow"
                ),
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--no-first-run",
                ],
                accept_downloads=False,
                viewport={"width": 1280, "height": 800},
            )
        except Exception as exc:
            raise BrowserUnavailable("Could not start Chromium: %s" % exc) from exc
        await self._prepare_context(self._context)
        self._contexts = {"default": self._context}
        pages = self._context.pages
        base = pages[0] if pages else await self._context.new_page()
        await self._trim_context_pages(self._context, base)
        self._attach_page(base)
        self._pages["default"] = base
        self._last_urls["default"] = base.url if base.url != "about:blank" else ""
        self._page = base
        self._active_session = "default"

    async def _connect_remote_page(self, session: str = "default") -> Any:
        from playwright.async_api import async_playwright

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._remote_browser = None
        if self._local_browser is not None:
            try:
                await self._local_browser.close()
            except Exception:
                pass
        self._local_browser = None
        self._context = None
        self._contexts = {}
        self._page = None
        self._pages = {}
        self._last_urls = {}
        self._remote = False

        self._playwright = await async_playwright().start()
        try:
            self._remote_browser = await self._playwright.chromium.connect_over_cdp(
                self.settings.browser_cdp_url,
                timeout=self.settings.browser_connect_timeout_ms,
            )
        except Exception as exc:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
            raise BrowserUnavailable(
                "Could not connect to the browser instance at %s: %s"
                % (self.settings.browser_cdp_url, exc)
            ) from exc

        self._remote = True
        contexts = self._remote_browser.contexts
        if contexts:
            self._context = contexts[0]
        else:
            self._context = await self._remote_browser.new_context(
                viewport={"width": 1280, "height": 800},
                accept_downloads=False,
            )
        self._context.set_default_timeout(15_000)
        await self._context.route("**/*", self._guard_route)
        self._context.on("page", self._attach_page)
        pages = self._context.pages
        for existing_page in pages:
            self._attach_page(existing_page)
        base = pages[0] if pages else await self._context.new_page()
        await self._trim_context_pages(self._context, base)
        self._attach_page(base)
        self._pages["default"] = base
        self._last_urls["default"] = base.url if base.url != "about:blank" else ""
        self._contexts = {"default": self._context}
        if session == "default":
            page = base
        else:
            context = await self._context_for(session)
            page = await context.new_page()
            self._attach_page(page)
            self._pages[session] = page
            self._last_urls[session] = ""
        self._page = page
        if (
            self.settings.browser_block_service_workers
            and not self._remote_sw_warned
        ):
            self._remote_sw_warned = True
            logger.info(
                "Remote browser controls service workers; block them in the browser "
                "container launch flags as well."
            )
        return page

    async def _guard_route(self, route: Any, request: Any) -> None:
        url = request.url
        if not url.startswith(("http://", "https://")):
            await route.continue_()
            return
        try:
            validate_url(
                url,
                self.settings.allowed_domains,
                self.settings.browser_allow_private_networks,
                resolve=False,
            )
        except ValueError:
            await route.abort()
            return
        if await self._inject_cors(route, request):
            return
        await route.continue_()

    def _cors_domain_match(self, host: str) -> bool:
        host = (host or "").lower().rstrip(".")
        return bool(host) and any(
            host == domain or host.endswith("." + domain)
            for domain in self.settings.cors_domains
        )

    async def _inject_cors(self, route: Any, request: Any) -> bool:
        if self.settings.browser_cors_mode != "inject":
            return False
        origin = request.headers.get("origin", "")
        if not origin:
            return False
        origin_host = (urlparse(origin).hostname or "").lower()
        target_host = (urlparse(request.url).hostname or "").lower()
        if not self._cors_domain_match(origin_host) or not self._cors_domain_match(
            target_host
        ):
            return False
        if self.vault is not None and origin not in self._cors_audited:
            self._cors_audited.add(origin)
            try:
                self.vault.audit.log(
                    "browser",
                    "browser_cors_injection",
                    {"origin": origin, "url": request.url[:500]},
                )
            except Exception:
                logger.debug("Could not audit CORS injection", exc_info=True)
        try:
            if request.method == "OPTIONS":
                headers = {
                    "access-control-allow-origin": origin,
                    "access-control-allow-credentials": "true",
                    "access-control-allow-methods": request.headers.get(
                        "access-control-request-method", "GET, POST, PUT, PATCH, DELETE, OPTIONS"
                    ),
                    "access-control-allow-headers": request.headers.get(
                        "access-control-request-headers", "content-type, authorization"
                    ),
                    "access-control-max-age": "600",
                    "vary": "Origin",
                }
                await route.fulfill(status=204, headers=headers, body=b"")
                self.cors_injections += 1
                return True
            if request.resource_type not in ("xhr", "fetch"):
                return False
            response = await route.fetch()
            headers = dict(response.headers)
            headers["access-control-allow-origin"] = origin
            headers["access-control-allow-credentials"] = "true"
            vary = headers.get("vary", "")
            if "origin" not in vary.lower():
                headers["vary"] = (vary + (", " if vary else "") + "Origin").strip(", ")
            await route.fulfill(response=response, headers=headers)
            self.cors_injections += 1
            return True
        except Exception:
            logger.debug("CORS injection failed for %s", request.url, exc_info=True)
            try:
                await route.continue_()
                return True
            except Exception:
                return True

    async def clear_site(self, domain: str, session: str = "default") -> Dict[str, Any]:
        host = (domain or "").strip().lower()
        if "://" in host:
            host = (urlparse(host).hostname or "").lower()
        if not host or len(host) > 253:
            raise BrowserError("A valid domain is required.")
        async with self.lock:
            page = await self._ensure_page(session)
            try:
                cdp = await page.context.new_cdp_session(page)
            except Exception as exc:
                raise BrowserError("Could not open a site-data session: %s" % exc) from exc
            cleared = 0
            try:
                for scheme in ("https", "http"):
                    try:
                        await cdp.send(
                            "Storage.clearDataForOrigin",
                            {
                                "origin": "%s://%s" % (scheme, host),
                                "storageTypes": "all",
                            },
                        )
                        cleared += 1
                    except Exception:
                        logger.debug(
                            "Could not clear %s://%s", scheme, host, exc_info=True
                        )
            finally:
                try:
                    await cdp.detach()
                except Exception:
                    pass
            if not cleared:
                raise BrowserError("Could not clear site data for %s." % host)
            reloaded = False
            if (urlparse(page.url).hostname or "").lower() == host:
                try:
                    await page.reload(
                        wait_until="domcontentloaded",
                        timeout=self.settings.browser_nav_timeout_ms,
                    )
                    reloaded = True
                except Exception:
                    logger.debug("Reload after clear-site failed", exc_info=True)
            self.last_url = page.url
            return {"domain": host, "cleared": True, "reloaded": reloaded}

    async def _checked_url(
        self, url: str, allow_private: Optional[bool] = None
    ) -> str:
        if allow_private is None:
            allow_private = bool(self.settings.browser_allow_private_networks)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: validate_url(
                url,
                self.settings.allowed_domains,
                bool(allow_private),
                True,
            ),
        )

    async def _page_text(self, page: Any, limit: int = 3000) -> str:
        try:
            text = await page.inner_text("body")
        except Exception:
            return ""
        return _collapse(text, limit)

    def _remember(self, session: str, page: Any) -> None:
        self._last_urls[session] = page.url
        if session == self._active_session:
            self.last_url = page.url

    async def activate(self, session: str = "default") -> Dict[str, Any]:
        async with self.lock:
            page = await self._ensure_page(session)
            try:
                await page.bring_to_front()
            except Exception:
                pass
            self._active_session = session
            self._remember(session, page)
            return {
                "session": session,
                "url": page.url,
                "title": await page.title(),
                "sessions": self.status(session)["sessions"],
            }

    # -- actions ----------------------------------------------------------
    async def open(
        self,
        url: str,
        session: str = "default",
        focus: bool = False,
        allow_private: Optional[bool] = None,
    ) -> Dict[str, Any]:
        target = await self._checked_url(url, allow_private=allow_private)
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            if focus:
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                self._active_session = session
            try:
                await page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=self.settings.browser_nav_timeout_ms,
                )
            except Exception as exc:
                logger.warning("Navigation failed for %s: %s", target, exc)
                if "ERR_" in str(exc):
                    await asyncio.sleep(1.0)
                    try:
                        await page.goto(
                            target,
                            wait_until="domcontentloaded",
                            timeout=self.settings.browser_nav_timeout_ms,
                        )
                    except Exception as retry_exc:
                        raise BrowserError(
                            "Navigation failed: %s" % retry_exc
                        ) from retry_exc
                else:
                    raise BrowserError("Navigation failed: %s" % exc) from exc
            await asyncio.sleep(0.3)
            self._remember(session, page)
            return {
                "url": page.url,
                "title": await page.title(),
                "text": await self._page_text(page),
            }

    async def read(
        self, max_chars: int = 6000, session: str = "default"
    ) -> Dict[str, Any]:
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            limit = max(200, min(int(max_chars), 20_000))
            text = await self._page_text(page, limit)
            frame_parts: List[str] = []
            try:
                frames = await self._frame_list(page)
            except Exception:
                frames = [page]
            for frame in frames[1:]:
                try:
                    frame_text = await frame.inner_text("body")
                except Exception:
                    continue
                frame_text = _collapse(frame_text, max(400, limit // 2))
                if frame_text.strip():
                    frame_parts.append("[frame %s]\n%s" % (frame.url, frame_text))
            if frame_parts:
                text = (text + "\n\n" + "\n\n".join(frame_parts))[: limit * 2 + 2000]
            needs_js = False
            if len(text.strip()) < 120:
                try:
                    info = await page.evaluate(NEEDS_JS_JS) or {}
                    needs_js = bool(info.get("scripts")) and int(
                        info.get("text_length") or 0
                    ) < 120
                except Exception:
                    needs_js = False
            self._remember(session, page)
            result: Dict[str, Any] = {
                "url": page.url,
                "title": await page.title(),
                "text": text,
            }
            if needs_js:
                note = (
                    "This page rendered almost no text and is driven by JavaScript, "
                    "so its content is invisible to browser_read. Prefer the site's "
                    "API/JSON endpoints (check the network requests or <script> data), "
                    "or open the live view for a human."
                )
                result["needs_javascript"] = True
                result["note"] = note
                result["text"] = (text + "\n\n[needs-JS] " + note).strip()
            return result

    async def _candidates(self, frame: Any, target: str, kind: str) -> List[Any]:
        """Build locator candidates for one frame, ordered by reliability."""
        target = str(target or "").strip()
        if not target:
            return []
        candidates: List[Any] = []

        def add(locator: Any) -> None:
            if locator is not None:
                candidates.append(locator)

        if kind in ("auto", "css"):
            try:
                add(frame.locator(target))
            except Exception:
                pass
        if kind in ("auto", "role"):
            for role in ROLE_NAMES:
                try:
                    add(frame.get_by_role(role, name=target, exact=False))
                except Exception:
                    continue
        if kind in ("auto", "label"):
            try:
                add(frame.get_by_label(target, exact=False))
            except Exception:
                pass
        if kind in ("auto", "placeholder"):
            try:
                add(frame.get_by_placeholder(target, exact=False))
            except Exception:
                pass
        if kind in ("auto", "text"):
            try:
                add(frame.get_by_text(target, exact=False))
            except Exception:
                pass
        if kind in ("auto", "value"):
            escaped = _css_attr_value(target)
            for selector in (
                'input[value="%s" i]' % escaped,
                'input[alt="%s" i]' % escaped,
                '[title="%s" i]' % escaped,
                '[aria-label="%s" i]' % escaped,
            ):
                try:
                    add(frame.locator(selector))
                except Exception:
                    continue
        return candidates

    async def _frame_list(self, page: Any) -> List[Any]:
        frames: List[Any] = [page]
        try:
            main = page.main_frame
            for frame in page.frames:
                if frame is not main:
                    frames.append(frame)
        except Exception:
            pass
        return frames

    async def _resolve_target(
        self, page: Any, target: str, kind: str = "auto"
    ) -> Tuple[Optional[Any], Optional[Any]]:
        """Find a visible element for a target across the page and its iframes."""
        kind = (kind or "auto").strip().lower()
        if kind not in TARGET_KINDS:
            kind = "auto"
        fallback: Optional[Tuple[Any, Any]] = None
        for frame in await self._frame_list(page):
            for locator in await self._candidates(frame, target, kind):
                try:
                    if await locator.count() == 0:
                        continue
                except Exception:
                    continue
                first = locator.first
                try:
                    visible = await first.is_visible()
                except Exception:
                    visible = True
                if visible:
                    return frame, first
                if fallback is None:
                    fallback = (frame, first)
        return fallback if fallback else (None, None)

    async def click(
        self, target: str, session: str = "default", kind: str = "auto"
    ) -> Dict[str, Any]:
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            frame, locator = await self._resolve_target(page, target, kind)
            if locator is None:
                frame_count = len(await self._frame_list(page))
                raise BrowserError(
                    "No element matched %r on %s (searched %d frame(s) by CSS, "
                    "role, label, placeholder, visible text and input value). "
                    "Call browser_read to see the page text, or pass an explicit "
                    "CSS selector with kind='css'."
                    % (target, page.url, frame_count)
                )
            try:
                await locator.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass
            clicked = False
            errors: List[str] = []
            for force in (False, True):
                try:
                    await locator.click(timeout=5_000, force=force)
                    clicked = True
                    break
                except Exception as exc:
                    errors.append(str(exc)[:240])
            if not clicked:
                # A backdrop or animation may own the pointer; dismiss, retry.
                try:
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.3)
                    await locator.click(timeout=3_000, force=True)
                    clicked = True
                except Exception as exc:
                    errors.append(str(exc)[:240])
            if not clicked:
                # If the matched text sits inside a link, follow its href.
                href = ""
                try:
                    anchor = locator.locator("xpath=ancestor-or-self::a[1]")
                    if await anchor.count() > 0:
                        href = await anchor.first.get_attribute(
                            "href", timeout=2_000
                        ) or ""
                except Exception:
                    href = ""
                if href and not href.lower().startswith(
                    ("javascript:", "mailto:", "tel:", "#")
                ):
                    try:
                        await page.goto(
                            urljoin(page.url, href),
                            wait_until="domcontentloaded",
                            timeout=15_000,
                        )
                        clicked = True
                    except Exception as exc:
                        errors.append(str(exc)[:240])
            if not clicked:
                try:
                    await locator.dispatch_event("click", timeout=3_000)
                    clicked = True
                except Exception as exc:
                    errors.append(str(exc)[:240])
            if not clicked:
                hint = (
                    "Hint: if a modal or cookie banner covers the page, close it "
                    "first (click its close/confirm control, or press Escape via "
                    "browser_press)."
                )
                raise BrowserError(
                    "Could not click %r: %s %s"
                    % (target, "; ".join(errors[-2:]) or "no match", hint)
                )
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3_000)
            except Exception:
                pass
            await asyncio.sleep(0.4)
            self._remember(session, page)
            return {
                "url": page.url,
                "title": await page.title(),
                "text": await self._page_text(page, 1500),
            }

    async def type_text(
        self,
        selector: str,
        text: str,
        submit: bool = False,
        session: str = "default",
        kind: str = "auto",
    ) -> Dict[str, Any]:
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            frame, locator = await self._resolve_target(page, selector, kind)
            if locator is None:
                raise BrowserError(
                    "No input matched %r (searched CSS, role, label, placeholder, "
                    "visible text and value across %d frame(s))."
                    % (selector, len(await self._frame_list(page)))
                )
            try:
                await locator.click(timeout=5_000)
            except Exception:
                pass
            try:
                await locator.fill(text, timeout=10_000)
                if submit:
                    try:
                        await locator.press("Enter", timeout=3_000)
                    except Exception:
                        await page.keyboard.press("Enter")
                    await asyncio.sleep(0.6)
            except Exception as exc:
                raise BrowserError(
                    "Could not type into %r: %s" % (selector, exc)
                ) from exc
            self._remember(session, page)
            return {
                "url": page.url,
                "title": await page.title(),
                "text": await self._page_text(page, 1500),
            }

    async def press(self, key: str, session: str = "default") -> Dict[str, Any]:
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            try:
                await page.keyboard.press(key)
                await asyncio.sleep(0.4)
            except Exception as exc:
                raise BrowserError("Key press failed: %s" % exc) from exc
            self._remember(session, page)
            return {"url": page.url, "title": await page.title()}

    async def screenshot(self, session: str = "default") -> bytes:
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            try:
                data = await page.screenshot(type="png", full_page=False)
                self._remember(session, page)
                return data
            except Exception as exc:
                raise BrowserError("Screenshot failed: %s" % exc) from exc

    async def _detect_login_locked(self, page: Any) -> Dict[str, Any]:
        try:
            info = await page.evaluate(LOGIN_FIELDS_JS)
        except Exception as exc:
            raise BrowserError("Could not inspect the page: %s" % exc) from exc
        if not info or not (info.get("username") or info.get("password")):
            raise BrowserError(
                "No visible username or password field found. Open the login page first."
            )
        return info

    async def detect_login_fields(self, session: str = "default") -> Dict[str, Any]:
        """Return selectors for the visible username/password/submit controls."""
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            return await self._detect_login_locked(page)

    async def _otp_fields(
        self, page: Any
    ) -> Tuple[Optional[Dict[str, Any]], Any]:
        """Find a one-time-code challenge in the main page or a child frame."""
        try:
            info = await page.evaluate(OTP_FIELDS_JS)
        except Exception:
            info = None
        if info:
            return info, page
        main_frame = getattr(page, "main_frame", None)
        for frame in getattr(page, "frames", []) or []:
            if frame is main_frame:
                continue
            try:
                info = await frame.evaluate(OTP_FIELDS_JS)
            except Exception:
                continue
            if info:
                info = dict(info)
                info["frame"] = True
                return info, frame
        return None, page

    async def detect_otp_fields(self, session: str = "default") -> Dict[str, Any]:
        """Return selectors for a visible one-time-code challenge."""
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            info, _ = await self._otp_fields(page)
            if not info:
                return {
                    "url": page.url,
                    "title": "",
                    "otp": "",
                    "submit": "",
                    "segmented": False,
                    "text": await self._page_text(page, 1200),
                }
            return info

    async def submit_otp(
        self,
        otp: str,
        session: str = "default",
        otp_selector: str = "",
        submit_selector: str = "",
    ) -> Dict[str, Any]:
        """Submit a user-supplied one-time code without storing or returning it."""
        code = "".join(str(otp or "").split()).replace("-", "")
        if not 4 <= len(code) <= 20:
            raise BrowserError("A one-time code must be 4–20 characters.")
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            selector = otp_selector.strip()
            submit_selector = submit_selector.strip()
            segmented = False
            frame = page
            if not selector:
                fields, frame = await self._otp_fields(page)
                selector = str((fields or {}).get("otp") or "").strip()
                submit_selector = submit_selector or str(
                    (fields or {}).get("submit") or ""
                ).strip()
                segmented = bool((fields or {}).get("segmented"))
            if not selector:
                raise BrowserError(
                    "No visible one-time-code field was found (checked the page and its frames)."
                )
            try:
                if segmented:
                    locator = frame.locator(selector).first
                    await locator.click(timeout=10_000)
                    try:
                        await locator.press_sequentially(code, delay=60)
                    except AttributeError:  # pragma: no cover - older Playwright
                        await frame.keyboard.type(code, delay=60)
                else:
                    await frame.fill(selector, code, timeout=10_000)
                if submit_selector:
                    await frame.click(submit_selector, timeout=10_000)
                else:
                    await frame.press(selector, "Enter")
                await asyncio.sleep(1.0)
            except Exception as exc:
                raise BrowserError("Could not submit the one-time code: %s" % exc) from exc
            self._remember(session, page)
            return {
                "submitted": True,
                "url": page.url,
                "title": await page.title(),
                "text": await self._page_text(page, 1200),
            }

    @staticmethod
    def chat_otp_candidate(text: str) -> str:
        match = OTP_CHAT_RE.match(str(text or "").strip())
        if not match:
            return ""
        candidate = match.group(1).replace("-", "")
        if not any(char.isdigit() for char in candidate):
            return ""
        return candidate

    async def submit_chat_otp(
        self, text: str, session: str = "default"
    ) -> Optional[Dict[str, Any]]:
        """Submit a code the user typed in chat, when an OTP page is open.

        Returns ``None`` when the message is not an OTP-shaped message or the
        browser is not on an OTP page. The code is never included in the result.
        """
        candidate = self.chat_otp_candidate(text)
        if not candidate:
            return None
        session = (session or "default").strip()[:100] or "default"
        status = self.status(session)
        if not status.get("started") or not status.get("url"):
            return None
        fields = await self.detect_otp_fields(session)
        if not fields.get("otp"):
            return None
        try:
            result = await self.submit_otp(
                candidate,
                session=session,
                otp_selector=str(fields.get("otp") or ""),
                submit_selector=str(fields.get("submit") or ""),
            )
            return {"submitted": True, **result}
        except BrowserError as exc:
            return {
                "submitted": False,
                "url": fields.get("url") or status.get("url") or "",
                "error": str(exc),
            }

    async def _otp_challenge(self, page: Any) -> Dict[str, Any]:
        try:
            info = await page.evaluate(OTP_FIELDS_JS)
        except Exception:
            logger.debug("Could not inspect OTP challenge", exc_info=True)
            return {}
        return info if isinstance(info, dict) else {}

    async def _submit_totp(self, page: Any, secret: str) -> bool:
        if not secret:
            return False
        try:
            otp = totp_code(secret)
        except TotpError as exc:
            raise BrowserError(str(exc)) from exc
        selectors = (
            'input[autocomplete="one-time-code"]',
            'input[name="app_otp"]',
            'input[name*="otp" i]',
            'input[id*="otp" i]',
            'input[name*="code" i]',
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            for selector in selectors:
                try:
                    locator = page.locator(selector).first
                    if await locator.count() and await locator.is_visible():
                        await locator.fill(otp, timeout=5_000)
                        button = page.locator(
                            'button[type="submit"], input[type="submit"]'
                        ).first
                        if await button.count() and await button.is_visible():
                            await button.click(timeout=5_000)
                        else:
                            await page.keyboard.press("Enter")
                        await asyncio.sleep(1.0)
                        return True
                except Exception:
                    continue
            await asyncio.sleep(0.25)
        return False

    async def _submit_credential(
        self,
        page: Any,
        secret: Dict[str, Any],
        username_selector: str,
        password_selector: str,
        submit_selector: str,
    ) -> Tuple[int, bool, str]:
        if not password_selector and not username_selector:
            raise BrowserError(
                "Could not find a username or password field on this page."
            )
        filled = 0
        if username_selector and secret.get("username"):
            await page.fill(
                username_selector, str(secret["username"]), timeout=10_000
            )
            filled += 1

        if password_selector:
            await page.fill(
                password_selector, str(secret["password"]), timeout=10_000
            )
            filled += 1
            if submit_selector:
                await page.click(submit_selector, timeout=10_000)
            else:
                await page.press(password_selector, "Enter")
            await asyncio.sleep(1.0)
            return await self._finish_credential(page, secret, filled)

        if not filled:
            raise BrowserError(
                "No visible username or password field was found on this page."
            )

        # Multi-step SSO: username/email first, then a password page.
        if submit_selector:
            await page.click(submit_selector, timeout=10_000)
        else:
            await page.press(username_selector, "Enter")
        await asyncio.sleep(0.8)

        fields: Dict[str, Any] = {}
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                info = await page.evaluate(LOGIN_FIELDS_JS)
            except Exception:
                info = None
            if isinstance(info, dict) and info.get("password"):
                fields = info
                break
            await asyncio.sleep(0.4)
        if not fields.get("password"):
            raise BrowserError(
                "The username was submitted, but no password field appeared. "
                "The next login step may need manual interaction."
            )

        password_selector = str(fields.get("password") or "")
        await page.fill(
            password_selector, str(secret["password"]), timeout=10_000
        )
        filled += 1
        final_submit = str(fields.get("submit") or "")
        if final_submit:
            await page.click(final_submit, timeout=10_000)
        else:
            await page.press(password_selector, "Enter")
        await asyncio.sleep(1.0)
        return await self._finish_credential(page, secret, filled)

    async def _finish_credential(
        self, page: Any, secret: Dict[str, Any], filled: int
    ) -> Tuple[int, bool, str]:
        otp_submitted = False
        otp_error = ""
        try:
            otp_submitted = await self._submit_totp(
                page, str(secret.get("totp_secret") or "")
            )
        except BrowserError as exc:
            otp_error = str(exc)
        return filled, otp_submitted, otp_error

    async def fill_credential(
        self,
        label: str,
        username_selector: str = "",
        password_selector: str = "",
        submit_selector: str = "",
        session: str = "default",
    ) -> Dict[str, Any]:
        """Type a vault entry into the current page. Never returns secrets.

        If selectors are omitted (or stale), the visible login form is detected
        automatically. If ``label`` is empty, the vault entry bound to the
        current hostname is used.
        """
        session = (session or "default").strip()[:100] or "default"
        async with self.lock:
            page = await self._ensure_page(session)
            host = (urlparse(page.url).hostname or "").lower()
            if label:
                secret = self.vault.get_secret_by_label(label)
            else:
                secret = self.vault.secret_for_host(host)
                label = secret["label"]
            if not host_matches(host, secret.get("domains", [])):
                raise BrowserError(
                    "Refusing to autofill %r on %s: entry is bound to %s."
                    % (label, host or "an unknown page", ", ".join(secret["domains"]))
                )

            autodetected = False
            if not password_selector:
                fields = await self._detect_login_locked(page)
                username_selector = fields.get("username", "")
                password_selector = fields.get("password", "")
                submit_selector = fields.get("submit", "")
                autodetected = True

            try:
                filled, otp_submitted, otp_error = await self._submit_credential(
                    page,
                    secret,
                    username_selector,
                    password_selector,
                    submit_selector,
                )
            except Exception as first_error:
                if autodetected:
                    raise BrowserError("Autofill failed: %s" % first_error) from first_error
                # Stored selectors may be stale after a redesign; detect once.
                fields = await self._detect_login_locked(page)
                username_selector = fields.get("username", "")
                password_selector = fields.get("password", "")
                submit_selector = fields.get("submit", "")
                autodetected = True
                try:
                    filled, otp_submitted, otp_error = await self._submit_credential(
                        page,
                        secret,
                        username_selector,
                        password_selector,
                        submit_selector,
                    )
                except Exception as exc:
                    raise BrowserError("Autofill failed: %s" % exc) from exc

            otp_required = False
            otp_selector = ""
            otp_submit_selector = ""
            if not otp_submitted:
                challenge = await self._otp_challenge(page)
                if challenge.get("otp"):
                    otp_required = True
                    otp_selector = str(challenge.get("otp") or "")[:500]
                    otp_submit_selector = str(challenge.get("submit") or "")[:500]
            self._remember(session, page)
            self.vault.audit.log(
                "agent", "vault_autofill", {"label": label, "host": host}
            )
            return {
                "filled_fields": filled,
                "submitted": True,
                "submit_method": "button" if submit_selector else "enter",
                "autodetected": autodetected,
                "otp_submitted": otp_submitted,
                "otp_error": otp_error,
                "otp_required": otp_required,
                "otp_selector": otp_selector,
                "otp_submit_selector": otp_submit_selector,
                "entry_label": label,
                "url": page.url,
                "title": await page.title(),
                "text": await self._page_text(page, 1200),
            }

    async def close(self) -> None:
        async with self.lock:
            if self._remote:
                # The sidecar owns the browser process and its persistent profile.
                # Drop the CDP connection without closing the visible browser.
                self._context = None
                self._contexts = {}
                self._page = None
                self._remote = False
                self._remote_browser = None
                if self._playwright is not None:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                self._playwright = None
                self._pages = {}
                self._last_urls = {}
                self._active_session = "default"
                self._monitored_pages.clear()
                self._console_audit_seen.clear()
                self._cors_audited.clear()
                return
            try:
                if self._context is not None:
                    await self._context.close()
                for key, context in self._contexts.items():
                    if key != "default":
                        try:
                            await context.close()
                        except Exception:
                            pass
                if self._local_browser is not None:
                    await self._local_browser.close()
            except Exception:
                pass
            self._local_browser = None
            self._context = None
            self._contexts = {}
            self._page = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
            self._playwright = None
            self._pages = {}
            self._last_urls = {}
            self._active_session = "default"
            self._monitored_pages.clear()
            self._console_audit_seen.clear()
            self._cors_audited.clear()


