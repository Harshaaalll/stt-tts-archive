"""
Fusion Finance settlement-collection agent — Deepgram STT + Gemini LLM + Murf AI TTS.
"""

from typing import Optional

from fastapi import WebSocket

from voice_agent import AgentConfig, run_voice_agent


_FUSION_IDLE_PROMPTS = {
    1: "ask me if I am able to hear you",
    2: "ask me if I am still here",
    3: (
        "Tell me that you are not able to hear me, that you are "
        "disconnecting the call and will call back again"
    ),
}


async def run_agent_live_fintech_exotel(
    websocket: WebSocket,
    *,
    voice: Optional[str] = None,
    language: str = "en-US",
    system_instruction: Optional[str] = None,
    dynamic_instruction: Optional[str] = None,
    greeting_text: Optional[str] = None,
    speaking_rate: float = 1.0,
) -> None:
    """Entry point for the Deepgram+Gemini+Murf server (Fusion Finance calls)."""
    config = AgentConfig(
        name="fusion_mfi",
        system_instruction=system_instruction or "",
        dynamic_instruction=dynamic_instruction,
        language=language,
        voice=voice,
        idle_prompts=_FUSION_IDLE_PROMPTS,
        greeting_text=greeting_text,
        speaking_rate=speaking_rate,
    )
    await run_voice_agent(websocket, config)
