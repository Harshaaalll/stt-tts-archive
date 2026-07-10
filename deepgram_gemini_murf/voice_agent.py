"""
Deepgram STT + Vertex Gemini LLM (with explicit cache) + Murf AI TTS.
"""

import asyncio
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Dict, Optional
from urllib.parse import unquote

import aiohttp
from fastapi import WebSocket
from loguru import logger
import logging
logger.remove()
logger.add(sys.stderr, level="INFO")
logging.getLogger("pipecat").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndTaskFrame,
    Frame,
    InterimTranscriptionFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    TTSSpeakFrame,
    MetricsFrame,
    TranscriptionFrame,
)
from pipecat.metrics.metrics import (
    LLMUsageMetricsData,
    TTFBMetricsData,
    TTSUsageMetricsData,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.exotel import ExotelFrameSerializer
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat_murf_tts import MurfTTSService
from pipecat.services.google.vertex.llm import GoogleVertexLLMService

from google.genai.types import CreateCachedContentConfig
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_mute import FirstSpeechUserMuteStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

# Parent-project imports (sys.path set up by the entrypoint script).

from text_normalizer import normalize_text


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LANGUAGE_MAP = {
    "ar-XA": Language.AR, "bn-IN": Language.BN_IN, "cmn-CN": Language.CMN_CN,
    "de-DE": Language.DE_DE, "en-US": Language.EN_US, "en-GB": Language.EN_GB,
    "en-IN": Language.EN_IN, "en-AU": Language.EN_AU, "es-ES": Language.ES_ES,
    "es-US": Language.ES_US, "fr-FR": Language.FR_FR, "fr-CA": Language.FR_CA,
    "gu-IN": Language.GU_IN, "hi-IN": Language.HI_IN, "id-ID": Language.ID_ID,
    "it-IT": Language.IT_IT, "ja-JP": Language.JA_JP, "kn-IN": Language.KN_IN,
    "ko-KR": Language.KO_KR, "ml-IN": Language.ML_IN, "mr-IN": Language.MR_IN,
    "nl-NL": Language.NL_NL, "pl-PL": Language.PL_PL, "pt-BR": Language.PT_BR,
    "ru-RU": Language.RU_RU, "ta-IN": Language.TA_IN, "te-IN": Language.TE_IN,
    "th-TH": Language.TH_TH, "tr-TR": Language.TR_TR, "vi-VN": Language.VI_VN,
}


DEFAULT_IDLE_PROMPTS = {
    1: "ask me if I am able to hear you",
    2: "ask me if I am still here",
    3: (
        "Tell me that you are not able to hear me, that you are "
        "disconnecting the call and will call back again"
    ),
}


_TERMINATE_TOOL_DESCRIPTION = (
    "Ends the voice call permanently. Irreversible. "
    "STRICT PRECONDITIONS — ALL must hold before calling: "
    "(1) The MOST RECENT user message in the conversation contains "
    "an explicit goodbye phrase: 'bye', 'goodbye', 'hang up', "
    "'thanks bye', 'alvida', 'no that's all', 'nothing else', "
    "or similar. (2) At least 4 user turns have occurred. "
    "(3) The customer has explicitly confirmed they want to end. "
    "FORBIDDEN: NEVER call on turn 1, 2, or 3. NEVER call during "
    "identity verification. NEVER call because the system prompt "
    "mentions 'escalation' or 'graceful exit' — those are tone "
    "instructions, not tool triggers. The graceful exit speech is "
    "spoken by you; the call ends naturally when the customer hangs "
    "up. Only invoke this tool if you read the user's LITERAL last "
    "message and it contains a clear goodbye. If even slightly "
    "unsure, do NOT call this tool — just continue the conversation."
)


_MAX_TOKENS = 256


_MURF_VOICES = {
    "en-US-natalie", "en-US-cooper", "en-US-matthew", "en-US-clint",
    "en-IN-priya", "en-IN-aditya", "hi-IN-priya", "hi-IN-aman",
    "natalie", "cooper", "matthew", "clint", "priya", "aditya", "neha", "amit"
}


# ---------------------------------------------------------------------------
# Pricing — Deepgram STT (USD) + Vertex Gemini LLM (USD) + Murf AI TTS (USD)
# ---------------------------------------------------------------------------

PRICING = {
    # Deepgram — native USD per second
    "stt_deepgram": {
        "nova-2":          0.0058 / 60.0,   # ≈ $0.0000967 / sec  (Hindi OK)
        "nova-2-general":  0.0058 / 60.0,
        "nova-3":          0.0077 / 60.0,   # ≈ $0.0001283 / sec
        "nova-3-general":  0.0077 / 60.0,
        "_default":        0.0058 / 60.0,
    },
    # Murf TTS — native USD per character
    "tts_murf_per_char_usd": {
        "FALCON":   10.0 / 1_000_000.0,  # $10 / 1M characters
        "GEN2":     30.0 / 1_000_000.0,  # $30 / 1M characters
        "_default": 10.0 / 1_000_000.0,
    },
    # Vertex Gemini — native USD per 1M tokens.
    "llm_usd": {
        "gemini-2.5-flash":        {"input": 0.30,  "cache_read": 0.03,  "output": 2.50},
        "gemini-2.5-flash-lite":   {"input": 0.10,  "cache_read": 0.01,  "output": 0.40},
        "gemini-2.5-pro":          {"input": 1.25,  "cache_read": 0.13,  "output": 10.00},
        "gemini-3.1-flash-lite":   {"input": 0.25,  "cache_read": 0.025, "output": 1.50},
        "gemini-3.1-pro-preview":  {"input": 2.00,  "cache_read": 0.20,  "output": 12.00},
        "_default":                {"input": 0.30,  "cache_read": 0.03,  "output": 2.50},
    },
}


def _get_inr_per_usd() -> float:
    try:
        return float(os.getenv("INR_PER_USD", "84.0"))
    except ValueError:
        logger.warning("INR_PER_USD env var is not numeric; falling back to 84.0")
        return 84.0


def _compute_call_costs(
    *,
    call_duration_s: float,
    llm_prompt_tokens: int,
    llm_cache_read_tokens: int,
    llm_completion_tokens: int,
    tts_characters: int,
    llm_model: str,
    stt_model: str,
) -> dict:
    """Per-call cost in BOTH USD and INR."""
    inr_per_usd = _get_inr_per_usd()

    # STT — Deepgram per-audio-second (native USD).
    stt_per_sec_override = os.getenv("DEEPGRAM_STT_USD_PER_SEC")
    if stt_per_sec_override:
        try:
            stt_per_sec = float(stt_per_sec_override)
        except ValueError:
            stt_per_sec = PRICING["stt_deepgram"]["_default"]
    else:
        stt_per_sec = PRICING["stt_deepgram"].get(
            stt_model, PRICING["stt_deepgram"]["_default"]
        )
    stt_usd = call_duration_s * stt_per_sec
    stt_inr = stt_usd * inr_per_usd

    # TTS — Murf AI (native USD).
    tts_model = os.getenv("MURF_MODEL", "FALCON").upper()
    tts_rate = PRICING["tts_murf_per_char_usd"].get(
        tts_model, PRICING["tts_murf_per_char_usd"]["_default"]
    )
    tts_per_char_override = os.getenv("MURF_TTS_USD_PER_CHAR")
    if tts_per_char_override:
        try:
            tts_rate = float(tts_per_char_override)
        except ValueError:
            pass
    tts_usd = tts_characters * tts_rate
    tts_inr = tts_usd * inr_per_usd

    # Gemini LLM — native USD per 1M tokens.
    llm_rates = PRICING["llm_usd"].get(llm_model, PRICING["llm_usd"]["_default"])
    uncached_input = max(0, llm_prompt_tokens - llm_cache_read_tokens)
    llm_usd = (
        uncached_input * llm_rates["input"]
        + llm_cache_read_tokens * llm_rates["cache_read"]
        + llm_completion_tokens * llm_rates["output"]
    ) / 1_000_000.0
    llm_inr = llm_usd * inr_per_usd

    return {
        "inr_per_usd": inr_per_usd,
        "stt_cost_usd": stt_usd, "stt_cost_inr": stt_inr,
        "llm_cost_usd": llm_usd, "llm_cost_inr": llm_inr,
        "tts_cost_usd": tts_usd, "tts_cost_inr": tts_inr,
        "total_cost_usd": stt_usd + llm_usd + tts_usd,
        "total_cost_inr": stt_inr + llm_inr + tts_inr,
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    name: str
    system_instruction: str
    language: str = "en-US"
    voice: Optional[str] = None
    dynamic_instruction: Optional[str] = None
    extra_tools: Optional[ToolsSchema] = None
    extra_tool_handlers: Dict[
        str, Callable[[FunctionCallParams], Awaitable[None]]
    ] = field(default_factory=dict)
    idle_prompts: Dict[int, str] = field(
        default_factory=lambda: dict(DEFAULT_IDLE_PROMPTS)
    )
    speaking_rate: float = 1.0
    max_call_duration_secs: int = 240
    terminate_min_elapsed_secs: float = 20.0
    greeting_text: Optional[str] = None
    


# ---------------------------------------------------------------------------
# Frame processors
# ---------------------------------------------------------------------------

class TranscriptLogger(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            logger.info(f"[STT final]   {frame.text!r}")
        elif isinstance(frame, InterimTranscriptionFrame):
            logger.debug(f"[STT interim] {frame.text!r}")
        await self.push_frame(frame, direction)


class CallMetricsAccumulator(FrameProcessor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cache_read_tokens = 0
        self.tts_characters = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, LLMUsageMetricsData):
                    u = d.value
                    self.prompt_tokens += getattr(u, "prompt_tokens", 0) or 0
                    self.completion_tokens += getattr(u, "completion_tokens", 0) or 0
                    self.total_tokens += getattr(u, "total_tokens", 0) or 0
                    self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
                elif isinstance(d, TTSUsageMetricsData):
                    self.tts_characters += int(d.value or 0)
                elif isinstance(d, TTFBMetricsData):
                    logger.info(f"[TTFB] {d.processor}: {d.value * 1000:.0f}ms")
        await self.push_frame(frame, direction)


class TextNormalizerProcessor(FrameProcessor):
    def __init__(self, language: str, **kwargs):
        super().__init__(**kwargs)
        self.language = language

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSSpeakFrame):
            original_text = frame.text
            lang_str = self.language
            if hasattr(lang_str, "value"):
                lang_str = lang_str.value
            elif not isinstance(lang_str, str):
                lang_str = str(lang_str)
            normalized = normalize_text(original_text, lang_str)
            logger.info(f"[Text Normalizer] {original_text!r} -> {normalized!r}")
            if not any(c.isalpha() for c in normalized):
                logger.warning(f"[Text Normalizer] Discarding TTSSpeakFrame with non-alphabetic text: {normalized!r}")
                return
            try:
                frame.text = normalized
            except Exception:
                frame = TTSSpeakFrame(text=normalized)
        await self.push_frame(frame, direction)


# ---------------------------------------------------------------------------
# Service builders
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise ValueError(f"{name} env var is required")
    return val


def _get_bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def _build_deepgram_stt(language: Language) -> DeepgramSTTService:
    """Deepgram streaming STT."""
    stt_model = os.getenv("DEEPGRAM_MODEL", "nova-3")
    try:
        utterance_end = int(os.getenv("DEEPGRAM_UTTERANCE_END_MS", "800"))
    except ValueError:
        utterance_end = 800
    dg_language_env = os.getenv("DEEPGRAM_LANGUAGE")
    if dg_language_env:
        dg_language = dg_language_env
        logger.info(f"[deepgram] using DEEPGRAM_LANGUAGE={dg_language!r} (env override)")
    else:
        dg_language = "multi"
        logger.info(
            f"[deepgram] no DEEPGRAM_LANGUAGE set; defaulting to {dg_language!r} "
            f"(call language was {language.value!r})"
        )
    svc = DeepgramSTTService(
        api_key=_require_env("DEEPGRAM_API_KEY"),
        settings=DeepgramSTTService.Settings(
            model=stt_model,
            language=dg_language,
            punctuate=True,
            interim_results=True,
            utterance_end_ms=utterance_end,
        ),
    )
    _PARAMS_TO_STRIP = (
        "profanity_filter",
        "numerals",
        "smart_format",
        "detect_entities",
        "dictation",
        "utterance_end_ms",
    )
    _orig_build = svc._build_connect_kwargs

    def _build_connect_kwargs_filtered():
        kw = _orig_build()
        for k in _PARAMS_TO_STRIP:
            kw.pop(k, None)
        return kw

    svc._build_connect_kwargs = _build_connect_kwargs_filtered
    return svc


def _resolve_murf_voice_details(voice_arg: Optional[str]) -> dict:
    res = {
        "voice_id": "en-US-natalie",
        "style": os.getenv("MURF_STYLE", "Conversational"),
        "model": os.getenv("MURF_MODEL", "FALCON").upper(),
    }
    
    env_voice = os.getenv("MURF_VOICE_ID")
    if env_voice:
        try:
            parsed = json.loads(env_voice)
            if isinstance(parsed, dict):
                if "voice_id" in parsed:
                    res["voice_id"] = parsed["voice_id"]
                if "style" in parsed:
                    res["style"] = parsed["style"]
                if "model" in parsed:
                    res["model"] = parsed["model"].upper()
        except json.JSONDecodeError:
            res["voice_id"] = env_voice

    if voice_arg:
        res["voice_id"] = voice_arg

    return res


def _resolve_murf_voice(voice_arg: Optional[str]) -> str:
    details = _resolve_murf_voice_details(voice_arg)
    return details["voice_id"]


def _resolve_murf_rate(speaking_rate: float) -> int:
    try:
        from dotenv import load_dotenv
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(project_root, ".env")
        load_dotenv(dotenv_path=env_path, override=True)
    except Exception:
        pass

    # Check MURF_RATE env var first
    rate_env = os.getenv("MURF_RATE")
    if rate_env:
        try:
            return max(-50, min(int(rate_env), 50))
        except ValueError:
            pass

    # Check if rate is specified inside the JSON string of MURF_VOICE_ID
    env_voice = os.getenv("MURF_VOICE_ID")
    if env_voice:
        try:
            parsed = json.loads(env_voice)
            if isinstance(parsed, dict) and "rate" in parsed:
                return max(-50, min(int(parsed["rate"]), 50))
        except (json.JSONDecodeError, ValueError):
            pass

    rate = int((speaking_rate - 1.0) * 100)
    return max(-50, min(rate, 50))


def _resolve_murf_locale(language: Language) -> str:
    from pipecat.transcriptions.language import resolve_language
    LANGUAGE_MAP = {
        Language.HI_IN: "hi-IN",
        Language.EN_US: "en-US",
        Language.EN_IN: "en-IN",
        Language.EN_GB: "en-GB",
    }
    return resolve_language(language, LANGUAGE_MAP, use_base_code=False)


def _resolve_murf_pitch() -> int:
    try:
        from dotenv import load_dotenv
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(project_root, ".env")
        load_dotenv(dotenv_path=env_path, override=True)
    except Exception:
        pass

    # Check MURF_PITCH env var first
    pitch_env = os.getenv("MURF_PITCH")
    if pitch_env:
        try:
            return max(-50, min(int(pitch_env), 50))
        except ValueError:
            pass

    # Check if pitch is specified inside the JSON string of MURF_VOICE_ID
    env_voice = os.getenv("MURF_VOICE_ID")
    if env_voice:
        try:
            parsed = json.loads(env_voice)
            if isinstance(parsed, dict) and "pitch" in parsed:
                return max(-50, min(int(parsed["pitch"]), 50))
        except (json.JSONDecodeError, ValueError):
            pass

    return 0


def _resolve_murf_variation() -> int:
    try:
        from dotenv import load_dotenv
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_path = os.path.join(project_root, ".env")
        load_dotenv(dotenv_path=env_path, override=True)
    except Exception:
        pass

    # Check MURF_VARIATION env var first
    variation_env = os.getenv("MURF_VARIATION")
    if variation_env:
        try:
            return max(0, min(int(variation_env), 5))
        except ValueError:
            pass

    # Check if variation is specified inside the JSON string of MURF_VOICE_ID
    env_voice = os.getenv("MURF_VOICE_ID")
    if env_voice:
        try:
            parsed = json.loads(env_voice)
            if isinstance(parsed, dict) and "variation" in parsed:
                return max(0, min(int(parsed["variation"]), 5))
        except (json.JSONDecodeError, ValueError):
            pass

    return 1


def _build_murf_tts(
    voice: Optional[str],
    language: Language,
    speaking_rate: float,
) -> MurfTTSService:
    """Murf AI TTS."""
    details = _resolve_murf_voice_details(voice)
    rate = _resolve_murf_rate(speaking_rate)
    pitch = _resolve_murf_pitch()
    variation = _resolve_murf_variation()
    
    model = details["model"]
    if model not in ("FALCON", "GEN2"):
        model = "FALCON"
        
    locale = _resolve_murf_locale(language)

    tts = MurfTTSService(
        api_key=_require_env("MURF_API_KEY"),
        params=MurfTTSService.InputParams(
            voice_id=details["voice_id"],
            style=details["style"],
            rate=rate,
            pitch=pitch,
            sample_rate=8000,
            format="PCM",
            model=model,
            locale=locale,
            variation=variation,
        ),
    )
    return tts


class CachingGoogleVertexLLMService(GoogleVertexLLMService):
    """Vertex Gemini with explicit `CachedContent` for the system prompt."""

    def __init__(
        self,
        *,
        cache_ttl_seconds: int = 3600,
        cache_display_name: str = "sarvam_agent_system_prompt",
        cache_enabled: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_display_name = cache_display_name
        self._cache_enabled = cache_enabled
        self._cached_content_name: str | None = None
        self._cache_setup_lock = asyncio.Lock()
        self._cache_create_attempted = False
        self._original_system_instruction = self._settings.system_instruction

    async def _ensure_cache(self) -> None:
        if not self._cache_enabled or self._cached_content_name is not None:
            return
        if self._cache_create_attempted:
            return
        async with self._cache_setup_lock:
            if self._cached_content_name is not None or self._cache_create_attempted:
                return
            self._cache_create_attempted = True
            sys_prompt_len = len(self._original_system_instruction or "")
            tools = getattr(self, "_tools", None) or None
            tool_config = getattr(self, "_tool_config", None) or None

            logger.info(
                f"[explicit-cache] attempting create: model={self._settings.model} "
                f"sys_prompt_chars={sys_prompt_len} "
                f"tools={len(tools) if tools else 0} "
                f"ttl={self._cache_ttl_seconds}s"
            )
            try:
                config_kwargs = dict(
                    system_instruction=self._original_system_instruction,
                    ttl=f"{self._cache_ttl_seconds}s",
                    display_name=self._cache_display_name,
                )
                if tools:
                    config_kwargs["tools"] = tools
                if tool_config:
                    config_kwargs["tool_config"] = tool_config
                cached = await self._client.aio.caches.create(
                    model=self._settings.model,
                    config=CreateCachedContentConfig(**config_kwargs),
                )
                self._cached_content_name = cached.name
                logger.info(
                    f"[explicit-cache] CREATED ✓ name={cached.name} "
                    f"ttl={self._cache_ttl_seconds}s model={self._settings.model}"
                )
            except Exception as e:
                logger.error(
                    f"[explicit-cache] CREATE FAILED ✗ {type(e).__name__}: {e} "
                    f"— falling back to implicit caching"
                )
                import traceback
                logger.error(f"[explicit-cache] traceback:\n{traceback.format_exc()}")

    async def _stream_content(self, context):
        await self._ensure_cache()
        response = await super()._stream_content(context)
        return self._wrap_response_max_metrics(response)

    async def _wrap_response_max_metrics(self, response):
        max_cache = 0
        max_prompt = 0
        max_completion = 0
        max_total = 0
        async for chunk in response:
            meta = getattr(chunk, "usage_metadata", None)
            if meta is not None:
                curr_cache = getattr(meta, "cached_content_token_count", None) or 0
                curr_prompt = getattr(meta, "prompt_token_count", None) or 0
                curr_completion = getattr(meta, "candidates_token_count", None) or 0
                curr_total = getattr(meta, "total_token_count", None) or 0
                if curr_cache > max_cache:
                    max_cache = curr_cache
                if curr_prompt > max_prompt:
                    max_prompt = curr_prompt
                if curr_completion > max_completion:
                    max_completion = curr_completion
                if curr_total > max_total:
                    max_total = curr_total
                try:
                    meta.cached_content_token_count = max_cache
                    meta.prompt_token_count = max_prompt
                    meta.candidates_token_count = max_completion
                    meta.total_token_count = max_total
                except (AttributeError, TypeError):
                    pass
            yield chunk

    def _build_generation_params(
        self,
        system_instruction=None,
        tools=None,
        tool_config=None,
    ):
        if self._cached_content_name:
            params = super()._build_generation_params(
                system_instruction=None,
                tools=None,
                tool_config=None,
            )
            params["cached_content"] = self._cached_content_name
            return params
        return super()._build_generation_params(
            system_instruction=system_instruction,
            tools=tools,
            tool_config=tool_config,
        )


def _build_google_llm(system_instruction: str) -> GoogleVertexLLMService:
    cache_enabled = os.getenv("GOOGLE_EXPLICIT_CACHE", "true").lower() in (
        "true", "1", "yes", "on"
    )
    cache_ttl = int(os.getenv("GOOGLE_CACHE_TTL_SECONDS", "3600"))
    return CachingGoogleVertexLLMService(
        credentials_path=os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        project_id=_require_env("GCP_PROJECT_ID"),
        location=os.getenv("GCP_LOCATION", "asia-southeast1"),
        cache_enabled=cache_enabled,
        cache_ttl_seconds=cache_ttl,
        settings=GoogleVertexLLMService.Settings(
            model=os.getenv("GOOGLE_MODEL", "gemini-2.5-flash"),
            system_instruction=system_instruction,
            max_tokens=_MAX_TOKENS,
        ),
    )


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------

def _build_transport(websocket: WebSocket) -> FastAPIWebsocketTransport:
    return FastAPIWebsocketTransport(
        websocket,
        params=FastAPIWebsocketParams(
            serializer=ExotelFrameSerializer(stream_sid="1"),
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
    )


def _build_vad() -> SileroVADAnalyzer:
    try:
        confidence = float(os.getenv("VAD_CONFIDENCE", "0.8"))
    except ValueError:
        confidence = 0.8
    try:
        start_secs = float(os.getenv("VAD_START_SECS", "0.3"))
    except ValueError:
        start_secs = 0.3
    try:
        stop_secs = float(os.getenv("VAD_STOP_SECS", "0.2"))
    except ValueError:
        stop_secs = 0.2
    return SileroVADAnalyzer(
        sample_rate=8000,
        params=VADParams(
            confidence=confidence,
            start_secs=start_secs,
            stop_secs=stop_secs,
        ),
    )


def _build_aggregators(vad: Optional[SileroVADAnalyzer], context: LLMContext, is_sarvam: bool = False):
    if vad is None:
        timeout = 0.0 if is_sarvam else 0.25
        stop_strategy = SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=timeout)
    else:
        stop_strategy = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())

    return LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad,
            user_idle_timeout=8.0,
            user_turn_strategies=UserTurnStrategies(
                stop=[stop_strategy],
            ),
            user_mute_strategies=[FirstSpeechUserMuteStrategy()],
        ),
    )


def _build_tools_schema(extra_tools: Optional[ToolsSchema]) -> ToolsSchema:
    terminate = FunctionSchema(
        name="terminate_call",
        description=_TERMINATE_TOOL_DESCRIPTION,
        properties={},
        required=[],
    )
    extras = list(extra_tools.standard_tools) if extra_tools else []
    return ToolsSchema(standard_tools=[terminate] + extras)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_voice_agent(websocket: WebSocket, config: AgentConfig) -> None:
    explicit_cache_on = os.getenv("GOOGLE_EXPLICIT_CACHE", "true").lower() in (
        "true", "1", "yes", "on"
    )
    logger.info(
        f"Starting {config.name} call [DEEPGRAM+GEMINI+MURF] | "
        f"lang={config.language} "
        f"stt={os.getenv('DEEPGRAM_MODEL', 'nova-3')} "
        f"llm={os.getenv('GOOGLE_MODEL', 'gemini-2.5-flash')} "
        f"explicit_cache={'on' if explicit_cache_on else 'off'} "
        f"tts_model={os.getenv('MURF_MODEL', 'FALCON')} "
        f"tts_voice={_resolve_murf_voice(config.voice)} "
        f"tts_rate={_resolve_murf_rate(config.speaking_rate)} "
        f"tts_pitch={_resolve_murf_pitch()} "
        f"tts_variation={_resolve_murf_variation()}"
    )

    initial_data = await websocket.receive_json()
    logger.info(f"Initial Exotel data: {initial_data}")
    if initial_data.get("event") != "connected":
        await websocket.close(code=1000)
        return

    pc_lang = _LANGUAGE_MAP.get(config.language, Language.EN_US)
    call_started_at = time.time()

    async def terminate_call(params: FunctionCallParams):
        elapsed = time.time() - call_started_at
        if elapsed < config.terminate_min_elapsed_secs:
            logger.warning(
                f"terminate_call BLOCKED at {elapsed:.1f}s — too early, "
                "treating as LLM hallucination. Call continues."
            )
            await params.result_callback(
                {"status": "blocked", "reason": "premature_termination"}
            )
            return
        logger.info(f"terminate_call invoked at {elapsed:.1f}s — ending pipeline")
        await params.result_callback({"status": "call_ended"})
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    transport = _build_transport(websocket)
    use_local_vad = _get_bool_env("USE_LOCAL_VAD", True)
    vad = _build_vad() if use_local_vad else None
    stt = _build_deepgram_stt(pc_lang)
    llm = _build_google_llm(config.system_instruction)
    tts_service = _build_murf_tts(
        config.voice, pc_lang, config.speaking_rate
    )
    text_normalizer = TextNormalizerProcessor(pc_lang)

    tools = _build_tools_schema(config.extra_tools)
    llm.register_function("terminate_call", terminate_call)
    for name, handler in config.extra_tool_handlers.items():
        llm.register_function(name, handler)

    initial_messages = []
    if config.dynamic_instruction:
        initial_messages.append(
            {"role": "user", "content": config.dynamic_instruction}
        )
    context = LLMContext(messages=initial_messages, tools=tools)
    user_aggregator, assistant_aggregator = _build_aggregators(vad, context)

    metrics_accumulator = CallMetricsAccumulator()
    transcript_logger = TranscriptLogger()
    

    idle_retry_count = {"n": 0}

    @user_aggregator.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        idle_retry_count["n"] += 1
        n = idle_retry_count["n"]
        logger.info(f"User idle (retry={n})")
        if n in config.idle_prompts:
            await aggregator.push_frame(
                LLMMessagesAppendFrame(
                    [{"role": "user", "content": config.idle_prompts[n]}],
                    run_llm=True,
                )
            )
        else:
            await aggregator.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)

    stages = [
        transport.input(),
        stt,
        transcript_logger,
        user_aggregator,
        llm,
        text_normalizer,
        tts_service,
    ]

    stages.extend([
        metrics_accumulator,
        transport.output(),
        assistant_aggregator,
    ])
    pipeline = Pipeline(stages)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )

    async def enforce_hard_limit(seconds: int):
        await asyncio.sleep(seconds)
        logger.warning(f"Hard call cap hit at {seconds}s — cancelling pipeline")
        await task.cancel()

    timeout_handle = asyncio.create_task(
        enforce_hard_limit(config.max_call_duration_secs)
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Exotel client connected — handing off to LLM")
        if config.greeting_text:
            logger.info(f"Playing one-time greeting: {config.greeting_text!r}")
            await task.queue_frame(TTSSpeakFrame(config.greeting_text))
        await task.queue_frame(LLMRunFrame())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Exotel client disconnected")
        await task.cancel()

    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        timeout_handle.cancel()
        call_duration_s = time.time() - call_started_at
        await export_call_token_usage(
            websocket,
            metrics_accumulator,
            agent_name=config.name,
            call_duration_s=call_duration_s,
        )


# ---------------------------------------------------------------------------
# Token usage CSV export
# ---------------------------------------------------------------------------

async def export_call_token_usage(
    websocket: WebSocket,
    metrics: CallMetricsAccumulator,
    agent_name: str = "default_agent",
    call_duration_s: float = 0.0,
) -> None:
    """Append one row to token_logs/<date>/token_logs_<agent>_deepgram_gemini_murf.csv."""
    try:
        path = getattr(websocket.url, "path", "") or ""
        custom_field = unquote(path.split("/")[-1]) if path else "Unknown"

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        llm_model = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
        stt_model = os.getenv("DEEPGRAM_MODEL", "nova-3")

        costs = _compute_call_costs(
            call_duration_s=call_duration_s,
            llm_prompt_tokens=metrics.prompt_tokens,
            llm_cache_read_tokens=metrics.cache_read_tokens,
            llm_completion_tokens=metrics.completion_tokens,
            tts_characters=metrics.tts_characters,
            llm_model=llm_model,
            stt_model=stt_model,
        )

        project_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        log_dir = os.path.join(project_root, "token_logs", date_str)
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"token_logs_{agent_name}_deepgram_gemini_murf.csv")
        file_exists = os.path.exists(log_file)

        if call_duration_s > 0:
            total_inr_per_min = (
                costs["total_cost_inr"] * 60.0 / call_duration_s
            )
        else:
            total_inr_per_min = 0.0

        with open(log_file, mode="a", newline="") as f:
            fieldnames = [
                "Date", "Time", "Custom Field",
                "Call Duration (s)", "INR per USD",
                "STT Model", "STT Seconds",
                "STT Cost (USD)", "STT Cost (INR)",
                "LLM Prompt Tokens", "LLM Cache Read Tokens",
                "LLM Completion Tokens", "LLM Total Tokens",
                "LLM Cost (USD)", "LLM Cost (INR)",
                "TTS Characters", "TTS Cost (USD)", "TTS Cost (INR)",
                "Total Tokens", "Total Cost (USD)", "Total Cost (INR)",
                "Total Cost per Minute (INR)",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "Date": date_str,
                "Time": time_str,
                "Custom Field": custom_field,
                "Call Duration (s)": round(call_duration_s, 2),
                "INR per USD": round(costs["inr_per_usd"], 4),
                "STT Model": stt_model,
                "STT Seconds": round(call_duration_s, 2),
                "STT Cost (USD)": round(costs["stt_cost_usd"], 6),
                "STT Cost (INR)": round(costs["stt_cost_inr"], 4),
                "LLM Prompt Tokens": metrics.prompt_tokens,
                "LLM Cache Read Tokens": metrics.cache_read_tokens,
                "LLM Completion Tokens": metrics.completion_tokens,
                "LLM Total Tokens": metrics.total_tokens,
                "LLM Cost (USD)": round(costs["llm_cost_usd"], 6),
                "LLM Cost (INR)": round(costs["llm_cost_inr"], 4),
                "TTS Characters": metrics.tts_characters,
                "TTS Cost (USD)": round(costs["tts_cost_usd"], 6),
                "TTS Cost (INR)": round(costs["tts_cost_inr"], 4),
                "Total Tokens": metrics.total_tokens,
                "Total Cost (USD)": round(costs["total_cost_usd"], 6),
                "Total Cost (INR)": round(costs["total_cost_inr"], 4),
                "Total Cost per Minute (INR)": round(total_inr_per_min, 4),
            })
        logger.info(
            f"Token usage exported to {log_file} | "
            f"call={call_duration_s:.1f}s @ ₹{costs['inr_per_usd']}/USD | "
            f"stt=${costs['stt_cost_usd']:.4f}/₹{costs['stt_cost_inr']:.4f} "
            f"llm=${costs['llm_cost_usd']:.4f}/₹{costs['llm_cost_inr']:.4f} "
            f"tts=${costs['tts_cost_usd']:.4f}/₹{costs['tts_cost_inr']:.4f} "
            f"total=${costs['total_cost_usd']:.4f}/₹{costs['total_cost_inr']:.4f} "
            f"(₹{total_inr_per_min:.2f}/min)"
        )
    except Exception as e:
        logger.error(f"Failed to export token usage: {e}")
