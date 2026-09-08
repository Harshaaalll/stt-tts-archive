"""Thin LLM layer over google-genai with structured output and token accounting.

Two things this deliberately does that a bare SDK call does not:

1.  Every call returns its cost alongside its result. Cost-per-resolution is a
    headline metric of this project, and metrics that are computed at the call
    site are the only ones that stay correct.
2.  It degrades to a deterministic offline mode when no key is configured, so
    the whole graph runs end to end on a fresh clone. A demo that needs three
    API keys before it prints anything is a demo nobody runs.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from .config import llm_cost

T = TypeVar("T", bound=BaseModel)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in ("1", "true")
    if not api_key and not use_vertex:
        return None
    from google import genai

    if use_vertex:
        _client = genai.Client(
            vertexai=True,
            project=os.getenv("GOOGLE_CLOUD_PROJECT"),
            location=os.getenv("GOOGLE_CLOUD_LOCATION", "asia-south1"),
        )
    else:
        _client = genai.Client(api_key=api_key)
    return _client


def is_offline() -> bool:
    return _get_client() is None


class LLMResult(BaseModel):
    parsed: dict
    cost: dict
    offline: bool = False


async def structured(
    *,
    model: str,
    system: str,
    user: str,
    schema: Type[T],
    temperature: float = 0.2,
    stage: str = "llm",
    offline_fallback: Optional[dict] = None,
) -> tuple[T, dict]:
    """Run a structured-output call. Returns (parsed_model, cost_entry)."""
    client = _get_client()
    if client is None:
        if offline_fallback is None:
            raise RuntimeError(
                f"No GOOGLE_API_KEY set and no offline fallback for stage {stage!r}"
            )
        return schema.model_validate(offline_fallback), {
            "stage": stage, "model": "offline", "usd": 0.0, "inr": 0.0,
            "prompt_tokens": 0, "output_tokens": 0,
        }

    from google.genai import types

    resp = await client.aio.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    usage = getattr(resp, "usage_metadata", None)
    cost = llm_cost(
        model,
        getattr(usage, "prompt_token_count", 0) or 0,
        getattr(usage, "candidates_token_count", 0) or 0,
    )
    cost["stage"] = stage

    parsed = getattr(resp, "parsed", None)
    if parsed is None:
        # Some models wrap JSON in a fence despite the mime type.
        text = (resp.text or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        parsed = schema.model_validate(json.loads(text))
    return parsed, cost


def format_citations(citations: list) -> str:
    """Render retrieved clauses for a prompt.

    The clause id is included in the rendered text on purpose: the drafting
    model is asked to cite ids, and a model cannot cite an identifier it was
    never shown.
    """
    return "\n\n".join(
        f"[{c.clause_id}] {c.heading}\n{c.text}" for c in citations
    )
