"""Pydantic request/response models for HiveTools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


# --- API Token Management ---


class TokenCreateRequest(BaseModel):
    name: str


class TokenCreateResponse(BaseModel):
    name: str
    raw_token: str
    created_at: int


class TokenInfo(BaseModel):
    id: int
    name: str
    created_at: int
    last_used_at: int | None
    revoked_at: int | None


# --- HITL ---


class HITLRequestResponse(BaseModel):
    hitl_request_id: str
    status: str


class HITLStatusResponse(BaseModel):
    status: str
    tool_name: str
    caller_mind: str
    decided_at: int | None


class HITLRespondRequest(BaseModel):
    #: Declared rather than `str`. An unrecognised action used to be taken as
    #: a denial, so `{"action": "approved"}` - the natural past-tense mistake
    #: - denied the request irrevocably and answered 200 saying `denied`.
    #: Refused by the model is better than refused by the route, because the
    #: body never reaches the decision at all.
    action: Literal["approve", "deny"]


# --- Tool Settings ---


class ToolSettingUpdate(BaseModel):
    mode: str
    sudo_timeout_minutes: int = 15


class ToolInfo(BaseModel):
    tool_name: str
    method: str
    path: str
    mode: str
    sudo_timeout_minutes: int


# --- Login ---


class LoginRequest(BaseModel):
    username: str
    password: str


# --- Gmail endpoint models ---


class GmailReadRequest(BaseModel):
    query: str
    max_results: int = 10


class GmailSendRequest(BaseModel):
    to: str
    subject: str
    body: str
    cc: str = ""


# --- Calendar endpoint models ---


class CalendarListRequest(BaseModel):
    query: str = ""
    time_min: str = ""
    time_max: str = ""
    max_results: int = 25
    calendar_id: str = "primary"


class CalendarCreateRequest(BaseModel):
    summary: str
    start_time: str
    end_time: str
    description: str = ""
    location: str = ""
    attendees: str = ""
    timezone: str = "America/Chicago"
    calendar_id: str = "primary"


# --- LinkedIn ---


class LinkedInPostRequest(BaseModel):
    content: str
    image_path: str = ""


# --- Docker Ops ---


class DockerOpsRequest(BaseModel):
    operation: str
    project: str
    service: str = ""
    tail: int = 50


# --- SMS ---


class SmsSendRequest(BaseModel):
    to: str
    body: str


class SmsInboundEnabledRequest(BaseModel):
    enabled: bool
