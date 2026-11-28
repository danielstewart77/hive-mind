"""Tests for Pydantic request/response models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_token_create_request_requires_name() -> None:
    from schemas import TokenCreateRequest

    with pytest.raises(ValidationError):
        TokenCreateRequest()  # type: ignore[call-arg]


def test_hitl_respond_request_validates_action() -> None:
    from schemas import HITLRespondRequest

    m1 = HITLRespondRequest(action="approve")
    assert m1.action == "approve"

    m2 = HITLRespondRequest(action="deny")
    assert m2.action == "deny"


def test_tool_setting_update_defaults() -> None:
    from schemas import ToolSettingUpdate

    m = ToolSettingUpdate(mode="sudo")
    assert m.sudo_timeout_minutes == 15


def test_gmail_send_request_cc_optional() -> None:
    from schemas import GmailSendRequest

    m = GmailSendRequest(to="a@b.com", subject="hi", body="hello")
    assert m.cc == ""
