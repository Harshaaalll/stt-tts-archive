"""Domain types shared by the graph, the connectors and the API."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _case_id() -> str:
    return f"case_{uuid.uuid4().hex[:10]}"


class Channel(str, Enum):
    REDDIT = "reddit"
    MOCK = "mock"
    PLAYSTORE = "playstore"
    VOICE = "voice"


class Category(str, Enum):
    BILLING = "billing"
    REFUND = "refund"
    SERVICE_OUTAGE = "service_outage"
    DELIVERY = "delivery"
    ACCOUNT_ACCESS = "account_access"
    AGENT_BEHAVIOUR = "agent_behaviour"
    DATA_PRIVACY = "data_privacy"
    PRAISE = "praise"
    OFF_TOPIC = "off_topic"


class Complaint(BaseModel):
    """One inbound item, normalised across connectors."""

    external_id: str
    channel: Channel
    author: str
    text: str
    url: Optional[str] = None
    created_at: str = Field(default_factory=_now)
    parent_text: Optional[str] = None  # thread context, when the connector has it


class Triage(BaseModel):
    is_complaint: bool
    category: Category
    severity: int = Field(ge=1, le=5, description="1 mild annoyance, 5 regulatory or safety risk")
    sentiment: Literal["angry", "frustrated", "neutral", "positive"]
    language: str = "en"
    summary: str
    entities: dict[str, Any] = Field(default_factory=dict)
    needs_private_data: bool = False


class Citation(BaseModel):
    """A retrieved policy clause. The clause_id is what makes the two channels
    provably consistent — the same id must appear behind the public reply and
    behind whatever the voice agent says."""

    clause_id: str
    doc: str
    heading: str
    text: str
    score: float


class Draft(BaseModel):
    text: str
    citations: list[str] = Field(default_factory=list)
    promises_compensation: bool = False


class GroundingVerdict(BaseModel):
    grounded: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    reasoning: str = ""


class Review(BaseModel):
    decision: Literal["approve", "edit", "reject"]
    final_text: Optional[str] = None
    reviewer: str = "human"
    note: str = ""
    decided_at: str = Field(default_factory=_now)
    auto: bool = False


class VoiceOutcome(BaseModel):
    happened: bool = False
    channel: Literal["webrtc", "exotel", "none"] = "none"
    duration_s: float = 0.0
    resolved: bool = False
    summary: str = ""
    citations_used: list[str] = Field(default_factory=list)
    transcript_path: Optional[str] = None


class CostEntry(BaseModel):
    stage: str
    model: str = ""
    usd: float = 0.0
    inr: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0


class ConsistencyReceipt(BaseModel):
    """The artefact that makes the product's claim auditable.

    Both channels answered from `shared_clauses`. `text_only` / `voice_only`
    being non-empty means the channels diverged — which is exactly the bug
    this system exists to catch.
    """

    shared_clauses: list[str]
    text_only: list[str]
    voice_only: list[str]
    consistent: bool
