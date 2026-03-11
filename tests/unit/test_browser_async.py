"""Unit tests for the async Playwright browser tool (tools/stateful/browser.py)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_playwright():
    """Provide a mock async Playwright environment."""
    page = AsyncMock()
    page.title = AsyncMock(return_value="Test Page")
    page.url = "https://example.com"
    page.goto = AsyncMock()
    page.click = AsyncMock()
    page.fill = AsyncMock()
    page.press = AsyncMock()
    page.inner_text = AsyncMock(return_value="Hello World")
    page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")
    page.wait_for_load_state = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.eval_on_selector_all = AsyncMock(return_value=[
        {"title": "Result 1", "url": "https://r1.com", "snippet": "snippet 1"},
    ])
    page.accessibility = MagicMock()
    page.accessibility.snapshot = AsyncMock(return_value={"role": "document", "name": "Test"})

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    pw_instance = AsyncMock()
    pw_instance.chromium = AsyncMock()
    pw_instance.chromium.launch = AsyncMock(return_value=browser)
    pw_instance.stop = AsyncMock()

    return {
        "pw": pw_instance,
        "browser": browser,
        "context": context,
        "page": page,
    }


@pytest.fixture(autouse=True)
def reset_browser_sessions():
    """Reset browser module state between tests."""
    import tools.stateful.browser as browser_mod
    browser_mod._sessions.clear()
    yield
    browser_mod._sessions.clear()


class TestBrowserNavigate:
    @pytest.mark.asyncio
    async def test_browser_navigate_returns_json_with_title_and_url(self, mock_playwright):
        """Asserts navigate returns JSON with title, url, content keys."""
        from tools.stateful.browser import browser_navigate, _sessions

        page = mock_playwright["page"]
        # Pre-populate session
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_navigate("https://example.com")
        data = json.loads(result)
        assert "title" in data
        assert "url" in data
        assert "content" in data
        assert data["title"] == "Test Page"

    @pytest.mark.asyncio
    async def test_browser_navigate_detects_captcha(self, mock_playwright):
        """Asserts captcha warning in response when page contains captcha indicators."""
        from tools.stateful.browser import browser_navigate, _sessions

        page = mock_playwright["page"]
        page.inner_text = AsyncMock(return_value="Please verify you are human")
        page.title = AsyncMock(return_value="CAPTCHA Check")
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_navigate("https://example.com")
        data = json.loads(result)
        assert data.get("captcha_detected") is True
        assert "warning" in data

    @pytest.mark.asyncio
    async def test_browser_navigate_error_returns_error_json(self, mock_playwright):
        """Asserts error JSON on navigation failure."""
        from tools.stateful.browser import browser_navigate, _sessions

        page = mock_playwright["page"]
        page.goto = AsyncMock(side_effect=Exception("Navigation failed"))
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_navigate("https://bad-url.com")
        data = json.loads(result)
        assert "error" in data


class TestBrowserClick:
    @pytest.mark.asyncio
    async def test_browser_click_returns_page_state(self, mock_playwright):
        """Asserts click returns JSON with title and url."""
        from tools.stateful.browser import browser_click, _sessions

        page = mock_playwright["page"]
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_click("#submit")
        data = json.loads(result)
        assert "title" in data
        assert "url" in data


class TestBrowserType:
    @pytest.mark.asyncio
    async def test_browser_type_returns_page_state(self, mock_playwright):
        """Asserts type returns JSON with title and url."""
        from tools.stateful.browser import browser_type, _sessions

        page = mock_playwright["page"]
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_type("#search", "hello")
        data = json.loads(result)
        assert "title" in data
        assert "url" in data

    @pytest.mark.asyncio
    async def test_browser_type_presses_enter_when_requested(self, mock_playwright):
        """Asserts Enter key pressed and page state returned."""
        from tools.stateful.browser import browser_type, _sessions

        page = mock_playwright["page"]
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_type("#search", "hello", press_enter=True)
        data = json.loads(result)
        page.press.assert_called_once_with("#search", "Enter")
        assert "title" in data


class TestBrowserContent:
    @pytest.mark.asyncio
    async def test_browser_content_text_mode(self, mock_playwright):
        """Asserts text mode returns inner text content."""
        from tools.stateful.browser import browser_content, _sessions

        page = mock_playwright["page"]
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_content(mode="text")
        data = json.loads(result)
        assert data["content"] == "Hello World"

    @pytest.mark.asyncio
    async def test_browser_content_accessibility_mode(self, mock_playwright):
        """Asserts accessibility mode returns body text content."""
        from tools.stateful.browser import browser_content, _sessions

        page = mock_playwright["page"]
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_content(mode="accessibility")
        data = json.loads(result)
        assert "content" in data
        assert isinstance(data["content"], str)
        assert data["content"] == "Hello World"

    @pytest.mark.asyncio
    async def test_browser_content_truncates_long_content(self, mock_playwright):
        """Asserts content over 12000 chars is truncated."""
        from tools.stateful.browser import browser_content, _sessions

        page = mock_playwright["page"]
        page.inner_text = AsyncMock(return_value="x" * 15000)
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_content(mode="text")
        data = json.loads(result)
        assert len(data["content"]) < 15000
        assert "[truncated]" in data["content"]


class TestBrowserScreenshot:
    @pytest.mark.asyncio
    async def test_browser_screenshot_returns_base64(self, mock_playwright):
        """Asserts base64 PNG screenshot in response."""
        from tools.stateful.browser import browser_screenshot, _sessions

        page = mock_playwright["page"]
        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_screenshot()
        data = json.loads(result)
        assert "screenshot_base64" in data
        assert data["media_type"] == "image/png"


class TestBrowserClose:
    @pytest.mark.asyncio
    async def test_browser_close_removes_session(self, mock_playwright):
        """Asserts session removed after close."""
        from tools.stateful.browser import browser_close, _sessions

        _sessions["default"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": mock_playwright["page"],
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_close("default")
        assert "default" not in _sessions
        assert "closed" in result.lower()

    @pytest.mark.asyncio
    async def test_browser_close_all_clears_all_sessions(self, mock_playwright):
        """Asserts all sessions cleared."""
        from tools.stateful.browser import browser_close, _sessions

        _sessions["s1"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": mock_playwright["page"],
            "last_used": asyncio.get_event_loop().time(),
        }
        _sessions["s2"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": mock_playwright["page"],
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await browser_close("all")
        assert len(_sessions) == 0
        assert "all" in result.lower()


class TestWebSearch:
    @pytest.mark.asyncio
    async def test_web_search_duckduckgo_returns_results(self, mock_playwright):
        """Asserts search returns JSON with query, engine, results."""
        from tools.stateful.browser import web_search, _sessions

        page = mock_playwright["page"]
        _sessions["_web_search"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await web_search("test query")
        data = json.loads(result)
        assert data["query"] == "test query"
        assert data["engine"] == "duckduckgo"
        assert "results" in data

    @pytest.mark.asyncio
    async def test_web_search_google_returns_results(self, mock_playwright):
        """Asserts Google engine returns results."""
        from tools.stateful.browser import web_search, _sessions

        page = mock_playwright["page"]
        _sessions["_web_search"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": page,
            "last_used": asyncio.get_event_loop().time(),
        }

        result = await web_search("test query", engine="google")
        data = json.loads(result)
        assert data["engine"] == "google"
        assert "results" in data


class TestIdleCleanup:
    @pytest.mark.asyncio
    async def test_idle_session_cleanup(self, mock_playwright):
        """Asserts sessions idle > timeout are cleaned up."""
        from tools.stateful.browser import _cleanup_idle, _sessions, _IDLE_TIMEOUT

        _sessions["old"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": mock_playwright["page"],
            "last_used": asyncio.get_event_loop().time() - _IDLE_TIMEOUT - 10,
        }
        _sessions["fresh"] = {
            "pw": mock_playwright["pw"],
            "browser": mock_playwright["browser"],
            "context": mock_playwright["context"],
            "page": mock_playwright["page"],
            "last_used": asyncio.get_event_loop().time(),
        }

        await _cleanup_idle()
        assert "old" not in _sessions
        assert "fresh" in _sessions
