"""Browser FastAPI router — migrated from hive_mind tools/stateful/browser.py.

Async Playwright browser automation. Sessions persist across requests and
auto-close after idle. HITL-gated on actions that introduce or modify state
(navigate, click, type, web_search). Read-only endpoints (content, screenshot,
close) require auth but skip HITL.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth_api import require_api_token
from hitl import hitl_gate

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

router = APIRouter(prefix="/browser", tags=["browser"])

# --- Session management ---
_sessions: dict[str, dict] = {}
_lock = asyncio.Lock()
_IDLE_TIMEOUT = 300  # 5 minutes


async def _get_or_create_session(session_key: str = "default"):
    """Get existing browser page or create a new one."""
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
        expired = [k for k, v in _sessions.items() if now - v["last_used"] > _IDLE_TIMEOUT]
        for k in expired:
            s = _sessions.pop(k)
            try:
                await s["context"].close()
                await s["browser"].close()
                await s["pw"].stop()
            except Exception:
                log.warning("Error cleaning up session '%s'", k)


async def _detect_captcha(page: Page) -> bool:
    """Check if the current page has CAPTCHA or bot detection."""
    indicators = ["captcha", "recaptcha", "hcaptcha", "robot", "bot detection", "verify you are human"]
    try:
        title = (await page.title() or "").lower()
        body_text = (await page.inner_text("body") or "").lower()
        combined = title + " " + body_text
        return any(indicator in combined for indicator in indicators)
    except Exception:
        return False


# --- Request schemas ---


class NavigateBody(BaseModel):
    url: str
    session_key: str = "default"


class ClickBody(BaseModel):
    selector: str
    session_key: str = "default"


class TypeBody(BaseModel):
    selector: str
    text: str
    press_enter: bool = False
    session_key: str = "default"


class WebSearchBody(BaseModel):
    query: str
    engine: str = "duckduckgo"
    max_results: int = 10


# --- Action endpoints (HITL-gated) ---


@router.post("/navigate")
async def navigate(
    body: NavigateBody,
    caller: str = Depends(hitl_gate("browser_navigate")),
) -> Any:
    """Navigate to a URL and return page title + content."""
    try:
        await _cleanup_idle()
        page = await _get_or_create_session(body.session_key)
        await page.goto(body.url, wait_until="networkidle", timeout=30000)
        body_text = await page.inner_text("body")
        if len(body_text) > 8000:
            body_text = body_text[:8000] + "\n... [truncated]"

        result: dict[str, Any] = {
            "title": await page.title(),
            "url": page.url,
            "content": body_text,
        }

        if await _detect_captcha(page):
            result["captcha_detected"] = True
            result["warning"] = (
                "CAPTCHA or bot detection detected. The site may require human verification."
            )
        return result
    except Exception as e:
        log.exception("browser_navigate failed")
        return {"error": str(e)}


@router.post("/click")
async def click(
    body: ClickBody,
    caller: str = Depends(hitl_gate("browser_click")),
) -> Any:
    """Click an element on the current page."""
    try:
        page = await _get_or_create_session(body.session_key)
        await page.click(body.selector, timeout=10000)
        await page.wait_for_load_state("networkidle", timeout=15000)
        return {"title": await page.title(), "url": page.url}
    except Exception as e:
        log.exception("browser_click failed")
        return {"error": str(e)}


@router.post("/type")
async def type_text(
    body: TypeBody,
    caller: str = Depends(hitl_gate("browser_type")),
) -> Any:
    """Type text into an input field."""
    try:
        page = await _get_or_create_session(body.session_key)
        await page.fill(body.selector, body.text, timeout=10000)
        if body.press_enter:
            await page.press(body.selector, "Enter")
            await page.wait_for_load_state("networkidle", timeout=15000)
        return {"title": await page.title(), "url": page.url}
    except Exception as e:
        log.exception("browser_type failed")
        return {"error": str(e)}


@router.post("/search")
async def web_search(
    body: WebSearchBody,
    caller: str = Depends(hitl_gate("browser_web_search")),
) -> Any:
    """Search the web via a real browser session."""
    try:
        page = await _get_or_create_session("_web_search")

        if body.engine == "google":
            await page.goto(
                f"https://www.google.com/search?q={quote_plus(body.query)}",
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
                f"https://html.duckduckgo.com/html/?q={quote_plus(body.query)}",
                timeout=30000,
            )
            await page.wait_for_load_state("networkidle")
            await page.wait_for_selector(".result.results_links", timeout=10000)
            results = await page.eval_on_selector_all(
                ".result.results_links",
                """els => els.map(el => {
                    const titleEl = el.querySelector('.result__a');
                    const rawHref = titleEl?.href || '';
                    let url = rawHref;
                    try {
                        const uddg = new URL(rawHref).searchParams.get('uddg');
                        if (uddg) url = decodeURIComponent(uddg);
                    } catch(e) {}
                    return {
                        title: titleEl?.textContent?.trim() || '',
                        url: url,
                        snippet: el.querySelector('.result__snippet')?.textContent?.trim() || ''
                    };
                }).filter(r => r.title && r.url)""",
            )

        return {
            "query": body.query,
            "engine": body.engine,
            "count": min(len(results), body.max_results),
            "results": results[:body.max_results],
        }
    except Exception as e:
        log.exception("browser_web_search failed")
        return {"error": str(e)}


# --- Read-only endpoints (auth only, no HITL) ---


@router.get("/content")
async def content(
    session_key: str = "default",
    mode: str = "accessibility",
    caller_mind: str = Depends(require_api_token),
) -> Any:
    """Get current page content. Read-only on an already-approved session."""
    try:
        page = await _get_or_create_session(session_key)
        text = await page.inner_text("body")
        if len(text) > 12000:
            text = text[:12000] + "\n... [truncated]"

        result: dict[str, Any] = {
            "title": await page.title(),
            "url": page.url,
            "content": text,
        }
        if await _detect_captcha(page):
            result["captcha_detected"] = True
            result["warning"] = (
                "CAPTCHA or bot detection detected. The site may require human verification."
            )
        return result
    except Exception as e:
        log.exception("browser_content failed")
        return {"error": str(e)}


@router.get("/screenshot")
async def screenshot(
    session_key: str = "default",
    caller_mind: str = Depends(require_api_token),
) -> Any:
    """Take a screenshot of the current page (base64 PNG)."""
    try:
        page = await _get_or_create_session(session_key)
        png_bytes = await page.screenshot(full_page=False)
        return {
            "title": await page.title(),
            "url": page.url,
            "screenshot_base64": base64.b64encode(png_bytes).decode(),
            "media_type": "image/png",
        }
    except Exception as e:
        log.exception("browser_screenshot failed")
        return {"error": str(e)}


@router.post("/close")
async def close_session(
    session_key: str = "default",
    caller_mind: str = Depends(require_api_token),
) -> Any:
    """Close a browser session and free resources. Use 'all' to close everything."""
    async with _lock:
        if session_key == "all":
            for s in _sessions.values():
                try:
                    await s["context"].close()
                    await s["browser"].close()
                    await s["pw"].stop()
                except Exception:
                    log.warning("Error closing session during 'all' cleanup")
            _sessions.clear()
            return {"status": "all sessions closed"}
        if session_key not in _sessions:
            return {"status": "not_found", "session_key": session_key}
        s = _sessions.pop(session_key)
        try:
            await s["context"].close()
            await s["browser"].close()
            await s["pw"].stop()
        except Exception:
            log.warning("Error closing session '%s'", session_key)
        return {"status": "closed", "session_key": session_key}
