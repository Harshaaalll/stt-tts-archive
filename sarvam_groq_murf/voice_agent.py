"""
Sarvam STT + Groq LLM (Llama-3.3-70B) + Murf AI TTS.
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
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat_murf_tts import MurfTTSService
from pipecat.services.groq.llm import GroqLLMService

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
from filler_classifier import SIMPLE_FILLERS as _DEFAULT_FILLER_PHRASES


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
# Pricing — Sarvam STT (INR-native) + Groq LLM (USD) + Murf AI TTS (USD)
# ---------------------------------------------------------------------------

PRICING = {
    # Sarvam — native INR.
    "stt_sarvam_per_sec_inr":  30.0 / 3600.0,
    # Murf TTS — native USD per character
    "tts_murf_per_char_usd": {
        "FALCON":   10.0 / 1_000_000.0,  # $10 / 1M characters
        "GEN2":     30.0 / 1_000_000.0,  # $30 / 1M characters
        "_default": 10.0 / 1_000_000.0,
    },
    # Groq — native USD per 1M tokens (no cache pricing tier).
    # Verify at https://groq.com/pricing before relying on these numbers.
    "llm_usd": {
        "llama-3.3-70b-versatile":                       {"input": 0.59,  "cache_read": 0.59,  "output": 0.79},
        "llama-3.1-8b-instant":                          {"input": 0.05,  "cache_read": 0.05,  "output": 0.08},
        "meta-llama/llama-4-scout-17b-16e-instruct":     {"input": 0.11,  "cache_read": 0.11,  "output": 0.34},
        "meta-llama/llama-4-maverick-17b-128e-instruct": {"input": 0.20,  "cache_read": 0.20,  "output": 0.60},
        "openai/gpt-oss-120b":                           {"input": 0.15,  "cache_read": 0.15,  "output": 0.75},
        "openai/gpt-oss-20b":                            {"input": 0.10,  "cache_read": 0.10,  "output": 0.50},
        "_default":                                      {"input": 0.59,  "cache_read": 0.59,  "output": 0.79},
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

    # STT — Sarvam STT (native INR).
    stt_inr = call_duration_s * PRICING["stt_sarvam_per_sec_inr"]
    stt_usd = stt_inr / inr_per_usd

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

    # Groq LLM — native USD per 1M tokens.
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


class FillerInjector(FrameProcessor):
    """Injects a TTSSpeakFrame downstream on demand. Placed AFTER the LLM so
    external `inject()` calls beat the LLM's own output to TTS."""

    async def inject(self, text: str):
        await self.push_frame(TTSSpeakFrame(text=text), FrameDirection.DOWNSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class LLMOutputSanitizer(FrameProcessor):
    """Strip `<speech>`/`</speech>` tags and drop `<start_of_turn>...` blocks
    from the streaming LLM text before it reaches TTS. Buffers a small tail
    so tag delimiters split across frames are still recognized.
    """

    _STRIP_TAGS = ("<speech>", "</speech>")
    _SUPPRESS_OPEN = "<start_of_turn>"
    _SUPPRESS_CLOSE = "</start_of_turn>"
    _MAX_HOLD = 20

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._buf = ""
        self._suppress = False

    def _consume(self, chunk: str, is_final: bool) -> str:
        self._buf += chunk
        out = []
        while self._buf:
            if self._suppress:
                idx = self._buf.find(self._SUPPRESS_CLOSE)
                if idx >= 0:
                    self._buf = self._buf[idx + len(self._SUPPRESS_CLOSE):]
                    self._suppress = False
                    continue
                if is_final:
                    self._buf = ""
                break
            lt = self._buf.find("<")
            if lt < 0:
                out.append(self._buf)
                self._buf = ""
                break
            if lt > 0:
                out.append(self._buf[:lt])
                self._buf = self._buf[lt:]
            matched = False
            for tag in self._STRIP_TAGS:
                if self._buf.startswith(tag):
                    self._buf = self._buf[len(tag):]
                    matched = True
                    break
            if matched:
                continue
            if self._buf.startswith(self._SUPPRESS_OPEN):
                self._buf = self._buf[len(self._SUPPRESS_OPEN):]
                self._suppress = True
                continue
            if ">" not in self._buf and len(self._buf) < self._MAX_HOLD and not is_final:
                break
            out.append("<")
            self._buf = self._buf[1:]
        return "".join(out)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        name = type(frame).__name__
        if name == "LLMTextFrame":
            cleaned = self._consume(getattr(frame, "text", "") or "", is_final=False)
            if cleaned:
                try:
                    frame.text = cleaned
                    await self.push_frame(frame, direction)
                except Exception:
                    from pipecat.frames.frames import LLMTextFrame as _LLMTextFrame
                    await self.push_frame(_LLMTextFrame(text=cleaned), direction)
            return
        if name == "LLMFullResponseEndFrame":
            tail = self._consume("", is_final=True)
            if tail:
                from pipecat.frames.frames import LLMTextFrame as _LLMTextFrame
                await self.push_frame(_LLMTextFrame(text=tail), direction)
            self._buf = ""
            self._suppress = False
        await self.push_frame(frame, direction)


class LatencyTimeline:
    """Per-turn latency timeline; resets on each new user speech start."""

    def __init__(self):
        self.t0: Optional[float] = None
        self.turn_id: int = 0
        self.marked: set = set()

    def reset(self):
        self.turn_id += 1
        self.t0 = None
        self.marked = set()

    def mark(self, key: str, extra: str = ""):
        if key in self.marked:
            return
        now = time.time()
        if self.t0 is None:
            self.t0 = now
        rel_ms = (now - self.t0) * 1000.0
        self.marked.add(key)
        suffix = f" {extra}" if extra else ""
        logger.info(f"[latency turn={self.turn_id}] {key} +{rel_ms:.0f}ms{suffix}")


class LatencyTracer(FrameProcessor):
    """Observes frames flowing past a pipeline position and marks stage timestamps."""

    def __init__(self, timeline: LatencyTimeline, **kwargs):
        super().__init__(**kwargs)
        self.timeline = timeline

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        name = type(frame).__name__
        if name == "UserStartedSpeakingFrame":
            self.timeline.reset()
        elif name in ("UserStoppedSpeakingFrame", "VADUserStoppedSpeakingFrame"):
            self.timeline.mark("user_stopped_speaking")
        elif name == "TranscriptionFrame":
            text = getattr(frame, "text", "") or ""
            self.timeline.mark("stt_final", f"text={text[:40]!r}")
        elif name == "LLMContextFrame":
            self.timeline.mark("llm_request_sent")
        elif name == "LLMFullResponseStartFrame":
            self.timeline.mark("llm_response_start")
        elif name == "LLMTextFrame":
            self.timeline.mark("llm_first_token")
        elif name == "TTSStartedFrame":
            self.timeline.mark("tts_started")
        elif name == "TTSAudioRawFrame":
            self.timeline.mark("tts_first_audio")
        elif name == "BotStartedSpeakingFrame":
            self.timeline.mark("bot_started_speaking")
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


_DEFAULT_FILLER_PAUSE_SUFFIX = "..."


def _get_filler_config():
    enabled = os.getenv("FILLER_ENABLED", "true").lower() in ("true", "1", "yes", "on")
    try:
        min_chars = int(os.getenv("FILLER_MIN_USER_CHARS", "15"))
    except ValueError:
        min_chars = 15
    phrases = list(_DEFAULT_FILLER_PHRASES)
    phrases_env = os.getenv("FILLER_PHRASES")
    if phrases_env:
        try:
            parsed = json.loads(phrases_env)
            if isinstance(parsed, list) and parsed and all(isinstance(p, str) for p in parsed):
                phrases = parsed
        except json.JSONDecodeError:
            pass
    suffix = os.getenv("FILLER_PAUSE_SUFFIX", _DEFAULT_FILLER_PAUSE_SUFFIX)
    phrases = [
        p if p.rstrip().endswith(suffix) else f"{p.rstrip()}{suffix}"
        for p in phrases
    ]
    return enabled, min_chars, phrases, suffix


def _get_bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def _build_sarvam_stt(language: Language) -> SarvamSTTService:
    """Sarvam streaming STT."""
    return SarvamSTTService(
        api_key=_require_env("SARVAM_API_KEY"),
        settings=SarvamSTTService.Settings(
            model=os.getenv("SARVAM_STT_MODEL", "saarika:v2.5"),
            language=language,
            vad_signals=True,
        ),
        keepalive_timeout=10.0,
        ttfs_p99_latency=0.4,
    )


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


def _build_groq_llm(system_instruction: str) -> GroqLLMService:
    """Groq's OpenAI-compatible streaming LLM. No prompt caching (Groq
    doesn't support it), so the full system prompt is sent every turn."""
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    logger.info(
        f"[groq] model={model} sys_prompt_chars={len(system_instruction or '')}"
    )
    return GroqLLMService(
        api_key=_require_env("GROQ_API_KEY"),
        settings=GroqLLMService.Settings(
            model=model,
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


def _build_aggregators(vad: Optional[SileroVADAnalyzer], context: LLMContext, is_sarvam: bool = True):
    if vad is None:
        timeout = 0.0 if is_sarvam else 0.05
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
            user_mute_strategies=[],
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
    logger.info(
        f"Starting {config.name} call [SARVAM+GROQ+MURF] | "
        f"lang={config.language} "
        f"stt={os.getenv('SARVAM_STT_MODEL', 'saarika:v2.5')} "
        f"llm={os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')} "
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
    logger.info(
        f"[barge-in] USE_LOCAL_VAD={use_local_vad} — "
        f"{'VAD-driven interruption ENABLED' if use_local_vad else 'NO local VAD; barge-in relies on STT UserStartedSpeakingFrame only'}"
    )
    vad = _build_vad() if use_local_vad else None
    stt = _build_sarvam_stt(pc_lang)
    llm = _build_groq_llm(config.system_instruction)
    tts_service = _build_murf_tts(
        config.voice, pc_lang, config.speaking_rate
    )
    text_normalizer = TextNormalizerProcessor(pc_lang)
    llm_sanitizer = LLMOutputSanitizer()
    filler_injector = FillerInjector()

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

    filler_enabled, filler_min_chars, filler_phrases, filler_suffix = _get_filler_config()
    filler_state = {"last_index": -1, "injected_phrases": set()}
    logger.info(
        f"[filler] enabled={filler_enabled} min_chars={filler_min_chars} "
        f"suffix={filler_suffix!r} phrases={filler_phrases}"
    )

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message):
        timeline.mark("smart_turn_aggregation_complete")
        logger.info(f"[Context User] Aggregated user turn text: {message.content!r}")
        if filler_enabled and filler_phrases:
            text = (message.content or "").strip()
            if len(text) >= filler_min_chars:
                idx = (filler_state["last_index"] + 1) % len(filler_phrases)
                filler_state["last_index"] = idx
                phrase = filler_phrases[idx]
                filler_state["injected_phrases"].add(phrase)
                logger.info(f"[filler] injecting {phrase!r} (user_text_len={len(text)})")
                await filler_injector.inject(phrase)

    def _strip_filler_prefix(content: str) -> str:
        if not content:
            return content
        stripped = content.lstrip()
        candidates = set(filler_state["injected_phrases"])
        for p in filler_phrases:
            candidates.add(p)
            candidates.add(p.rstrip(" ."))
            candidates.add(p.rstrip(" ." + filler_suffix))
        for p in list(candidates):
            candidates.add(p.replace("...", ".. ."))
        for cand in sorted(candidates, key=len, reverse=True):
            if cand and stripped.startswith(cand):
                stripped = stripped[len(cand):].lstrip(" .।")
                break
        return stripped

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message):
        original = message.content or ""
        cleaned = _strip_filler_prefix(original)
        if cleaned != original:
            logger.info(f"[filler-strip] {original[:60]!r} -> {cleaned[:60]!r}")
            try:
                message.content = cleaned
            except Exception:
                pass
            try:
                ctx = getattr(aggregator, "_context", None) or getattr(aggregator, "context", None)
                if ctx is not None and getattr(ctx, "messages", None):
                    last = ctx.messages[-1]
                    if isinstance(last, dict):
                        c = last.get("content")
                        if isinstance(c, str) and c.startswith(original[: min(len(original), 40)]):
                            last["content"] = cleaned
                    else:
                        c = getattr(last, "content", None)
                        if isinstance(c, str) and c.startswith(original[: min(len(original), 40)]):
                            try:
                                last.content = cleaned
                            except Exception:
                                pass
            except Exception as e:
                logger.warning(f"[filler-strip] could not mutate context: {e}")
        logger.info(f"[Context Assistant] Aggregated assistant turn text: {(message.content or '')!r}")

    metrics_accumulator = CallMetricsAccumulator()
    transcript_logger = TranscriptLogger()
    timeline = LatencyTimeline()
    tracer_stt = LatencyTracer(timeline)
    tracer_agg = LatencyTracer(timeline)
    tracer_tts = LatencyTracer(timeline)
    

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
        tracer_stt,
        user_aggregator,
        tracer_agg,
        llm,
        llm_sanitizer,
        # text_normalizer,  # TEMP: disabled for filler tuning
        filler_injector,
        tts_service,
        tracer_tts,
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
        else:
            # Only auto-run LLM when there's no canned greeting; otherwise the
            # LLM's opener races with the greeting audio and can cut it off.
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
    """Append one row to token_logs/<date>/token_logs_<agent>_sarvam_groq_murf.csv."""
    try:
        path = getattr(websocket.url, "path", "") or ""
        custom_field = unquote(path.split("/")[-1]) if path else "Unknown"

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        llm_model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        stt_model = os.getenv("SARVAM_STT_MODEL", "saarika:v2.5")

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
        log_file = os.path.join(log_dir, f"token_logs_{agent_name}_sarvam_groq_murf.csv")
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
