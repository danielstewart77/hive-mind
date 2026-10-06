"""`/models` lists what the host can actually serve, on every mind.

The command used to `import models_api` — a module that exists in exactly one
mind's repo. Installed as a shared package, that import raised on every other
mind, and it raised above the `try` meant to turn a failure into a sentence,
so the command answered nothing at all. The catalog is now something the host
hands over, which means the interesting cases are a host that handed over
nothing and a host whose proxy is unreachable.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hive_surfaces.telegram_bot as tb
from hive_surfaces.config import SurfaceConfig, config, configure


@pytest.fixture(autouse=True)
def _restore_config():
    before = config._installed()
    yield
    configure(before)


def _authorized_update():
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = 456
    update.message.reply_text = AsyncMock()
    return update


def _allow(**fields):
    configure(SurfaceConfig(telegram_allowed_users=[123], **fields))


class TestTheModelList:
    @pytest.mark.asyncio
    async def test_a_mind_with_no_catalog_says_so_instead_of_raising(self) -> None:
        """Which is every mind but one, before this changed."""
        _allow()
        update = _authorized_update()

        await tb.cmd_models(update, MagicMock())

        sent = update.message.reply_text.await_args.args[0]
        assert "no model list" in sent.lower()

    @pytest.mark.asyncio
    async def test_the_rows_the_host_supplied_are_what_is_listed(self) -> None:
        async def catalog():
            return [
                {"name": "claude-opus-5", "provider_label": "Azure"},
                {"name": "qwen3-coder", "provider_label": "Ollama", "label": "Qwen Coder"},
            ]

        _allow(models_catalog=catalog, default_model="claude-opus-5")
        update = _authorized_update()

        await tb.cmd_models(update, MagicMock())

        sent = update.message.reply_text.await_args.args[0]
        assert "claude-opus-5" in sent
        assert "Qwen Coder" in sent
        assert "Azure" in sent and "Ollama" in sent

    @pytest.mark.asyncio
    async def test_the_configured_default_is_marked_in_the_list(self) -> None:
        """A list of names with no indication of where the mind is pointed is
        a list that cannot answer the question it was asked."""
        async def catalog():
            return [{"name": "a", "provider": "p"}, {"name": "b", "provider": "p"}]

        _allow(models_catalog=catalog, default_model="b")
        update = _authorized_update()

        await tb.cmd_models(update, MagicMock())

        lines = update.message.reply_text.await_args.args[0].splitlines()
        marked = [line for line in lines if "default" in line]
        assert len(marked) == 1 and "`b`" in marked[0]

    @pytest.mark.asyncio
    async def test_an_unreachable_proxy_reads_as_nothing_offered(self) -> None:
        """Distinct from a mind that offers no list at all."""
        async def catalog():
            return []

        _allow(models_catalog=catalog)
        update = _authorized_update()

        await tb.cmd_models(update, MagicMock())

        assert "cannot reach its provider" in update.message.reply_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_a_failing_resolver_becomes_a_sentence_not_a_traceback(self) -> None:
        async def catalog():
            raise RuntimeError("401 from the proxy")

        _allow(models_catalog=catalog)
        update = _authorized_update()

        await tb.cmd_models(update, MagicMock())

        assert "401 from the proxy" in update.message.reply_text.await_args.args[0]

    @pytest.mark.asyncio
    async def test_an_unauthorized_user_is_told_nothing_about_the_models(self) -> None:
        async def catalog():
            return [{"name": "claude-opus-5", "provider": "Azure"}]

        configure(SurfaceConfig(telegram_allowed_users=[], models_catalog=catalog))
        update = _authorized_update()

        await tb.cmd_models(update, MagicMock())

        sent = update.message.reply_text.await_args.args[0]
        assert "claude-opus-5" not in sent
