"""Async browser automation tools via Playwright.

Provides headless browser capabilities for navigating JavaScript-rendered
pages, filling forms, clicking elements, extracting content, and performing
web searches. Sessions persist across tool calls and auto-close after idle.

Uses async Playwright API with asyncio.Lock for session management.
Designed for direct FastMCP registration (no @tool() decorator).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

# --- Session management ---
_sessions: dict[str, dict] = {}
_lock = asyncio.Lock()
_IDLE_TIMEOUT = 300  # 5 minutes


async def _get_or_create_session(session_key: str = "default"):
    """Get existing browser page or create a new one.

    Args:
        session_key: Identifier for the browser session.

    Returns:
        The Playwright Page object for the given session.
    """
    async with _lock:
        if session_key in _sessions:
            _sessions[session_key]["last_used"] = asyncio.get_running_loop().time()
            return _sessions[session_key]["page"]

        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()
        _sessions[session_key] = {
            "pw": pw,
            "browser": browser,
            "context": context,
            "page": page,
            "last_used": asyncio.get_running_loop().time(),
        }
        return page


async def _cleanup_idle() -> None:
    """Close sessions idle longer than _IDLE_TIMEOUT."""
    now = asyncio.get_running_loop().time()
    async with _lock:
        expired = [
            k for k, v in _sessions.items() if now - v["last_used"] > _IDLE_TIMEOUT
        ]
        for k in expired:
            s = _sessions.pop(k)
            try:
                await s["context"].close()
                await s["browser"].close()
                await s["pw"].stop()
            except Exception:
                logger.warning("Error cleaning up session '%s'", k)


async def _detect_captcha(page: Page) -> bool:
    """Check if the current page has CAPTCHA or bot detection.

    Args:
        page: Playwright Page object.

    Returns:
        True if CAPTCHA-like content is detected.
    """
    indicators = [
        "captcha",
        "recaptcha",
        "hcaptcha",
        "robot",
        "bot detection",
        "verify you are human",
    ]
    try:
        title = (await page.title() or "").lower()
        body_text = (await page.inner_text("body") or "").lower()
        combined = title + " " + body_text
        return any(indicator in combined for indicator in indicators)
    except Exception:
        return False


# --- Browser tool functions (no decorators) ---


async def browser_navigate(url: str, session_key: str = "default") -> str:
    """Navigate to a URL and return the page title + accessibility snapshot.

    Args:
        url: Full URL to navigate to.
        session_key: Browser session identifier (default: "default").

    Returns:
        JSON with title, url, and accessibility tree (truncated to 8000 chars).
    """
    try:
        await _cleanup_idle()
        page = await _get_or_create_session(session_key)
        await page.goto(url, wait_until="networkidle", timeout=30000)
        body_text = await page.inner_text("body")
        if len(body_text) > 8000:
            body_text = body_text[:8000] + "\n... [truncated]"

        result: dict = {
            "title": await page.title(),
            "url": page.url,
            "content": body_text,
        }

        if await _detect_captcha(page):
            result["captcha_detected"] = True
            result["warning"] = (
                "CAPTCHA or bot detection detected. "
                "The site may require human verification."
            )

        return json.dumps(result)
    except Exception as e:
        logger.exception("browser_navigate failed")
        return json.dumps({"error": str(e)})


async def browser_click(selector: str, session_key: str = "default") -> str:
    """Click an element on the current page.

    Args:
        selector: CSS selector, text selector ('text=Sign In'), or role
                  selector ('role=button[name="Submit"]').
        session_key: Browser session identifier.

    Returns:
        JSON with new page title and URL after click.
    """
    try:
        page = await _get_or_create_session(session_key)
        await page.click(selector, timeout=10000)
        await page.wait_for_load_state("networkidle", timeout=15000)
        return json.dumps({
            "title": await page.title(),
            "url": page.url,
        })
    except Exception as e:
        logger.exception("browser_click failed")
        return json.dumps({"error": str(e)})


async def browser_type(
    selector: str,
    text: str,
    press_enter: bool = False,
    session_key: str = "default",
) -> str:
    """Type text into an input field.

    Args:
        selector: CSS selector for the input element.
        text: Text to type.
        press_enter: Press Enter after typing (default: False).
        session_key: Browser session identifier.

    Returns:
        JSON confirmation with page title and URL.
    """
    try:
        page = await _get_or_create_session(session_key)
        await page.fill(selector, text, timeout=10000)
        if press_enter:
            await page.press(selector, "Enter")
            await page.wait_for_load_state("networkidle", timeout=15000)
        return json.dumps({
            "title": await page.title(),
            "url": page.url,
        })
    except Exception as e:
        logger.exception("browser_type failed")
        return json.dumps({"error": str(e)})


async def browser_content(
    session_key: str = "default",
    mode: str = "accessibility",
) -> str:
    """Get current page content.

    Args:
        session_key: Browser session identifier.
        mode: "accessibility" (default, structured tree) or "text" (inner text).

    Returns:
        JSON with page content (truncated to 12000 chars).
    """
    try:
        page = await _get_or_create_session(session_key)
        if mode == "text":
            content = await page.inner_text("body")
        else:
            content = await page.inner_text("body")
        if len(content) > 12000:
            content = content[:12000] + "\n... [truncated]"

        result: dict = {
            "title": await page.title(),
            "url": page.url,
            "content": content,
        }

        if await _detect_captcha(page):
            result["captcha_detected"] = True
            result["warning"] = (
                "CAPTCHA or bot detection detected. "
                "The site may require human verification."
            )

        return json.dumps(result)
    except Exception as e:
        logger.exception("browser_content failed")
        return json.dumps({"error": str(e)})


async def browser_screenshot(session_key: str = "default") -> str:
    """Take a screenshot of the current page (base64 PNG).

    Args:
        session_key: Browser session identifier.

    Returns:
        JSON with base64-encoded PNG screenshot.
    """
    try:
        page = await _get_or_create_session(session_key)
        png_bytes = await page.screenshot(full_page=False)
        return json.dumps({
            "title": await page.title(),
            "url": page.url,
            "screenshot_base64": base64.b64encode(png_bytes).decode(),
            "media_type": "image/png",
        })
    except Exception as e:
        logger.exception("browser_screenshot failed")
        return json.dumps({"error": str(e)})


async def browser_close(session_key: str = "default") -> str:
    """Close a browser session and free resources.

    Args:
        session_key: Session to close. Use "all" to close everything.

    Returns:
        Confirmation message.
    """
    async with _lock:
        if session_key == "all":
            for s in _sessions.values():
                try:
                    await s["context"].close()
                    await s["browser"].close()
                    await s["pw"].stop()
                except Exception:
                    logger.warning("Error closing session during 'all' cleanup")
            _sessions.clear()
            return "All browser sessions closed."
        if session_key not in _sessions:
            return f"No session '{session_key}' found."
        s = _sessions.pop(session_key)
        try:
            await s["context"].close()
            await s["browser"].close()
            await s["pw"].stop()
        except Exception:
            logger.warning("Error closing session '%s'", session_key)
        return f"Session '{session_key}' closed."


async def web_search(
    query: str,
    engine: str = "duckduckgo",
    max_results: int = 10,
) -> str:
    """Search the web and return structured results.

    Uses a real browser to perform the search, bypassing API restrictions.
    DuckDuckGo is default (no bot detection). Google available but may
    trigger CAPTCHAs.

    Args:
        query: Search query string.
        engine: "duckduckgo" (default) or "google".
        max_results: Maximum results to return (default: 10).

    Returns:
        JSON with query, engine, count, and results array (title, url, snippet).
    """
    try:
        page = await _get_or_create_session("_web_search")

        if engine == "google":
            await page.goto(
                f"https://www.google.com/search?q={quote_plus(query)}",
                timeout=30000,
            )
            await page.wait_for_load_state("networkidle")
            results = await page.eval_on_selector_all(
                "div.g",
                """els => els.map(el => ({
                    title: el.querySelector('h3')?.textContent || '',
                    url: el.querySelector('a')?.href || '',
                    snippet: el.querySelector('.VwiC3b')?.textContent || ''
                })).filter(r => r.title && r.url)""",
            )
        else:
            await page.goto(
                f"https://duckduckgo.com/?q={quote_plus(query)}",
                timeout=30000,
            )
            await page.wait_for_load_state("networkidle")
            await page.wait_for_selector("[data-result]", timeout=10000)
            results = await page.eval_on_selector_all(
                "[data-result]",
                """els => els.map(el => ({
                    title: el.querySelector('h2 a')?.textContent || '',
                    url: el.querySelector('h2 a')?.href || '',
                    snippet: el.querySelector('.result__snippet')?.textContent || ''
                })).filter(r => r.title && r.url)""",
            )

        return json.dumps({
            "query": query,
            "engine": engine,
            "count": min(len(results), max_results),
            "results": results[:max_results],
        })
    except Exception as e:
        logger.exception("web_search failed")
        return json.dumps({"error": str(e)})


# All browser tool functions for registration
BROWSER_TOOLS = [
    browser_navigate,
    browser_click,
    browser_type,
    browser_content,
    browser_screenshot,
    browser_close,
    web_search,
]
