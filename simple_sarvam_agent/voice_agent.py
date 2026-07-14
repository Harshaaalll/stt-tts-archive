"""Minimal Sarvam STT + Gemini LLM + Sarvam TTS pipeline.

Env vars:
    SARVAM_API_KEY            required (used for both STT and TTS)
    GCP_PROJECT_ID            required
    GOOGLE_APPLICATION_CREDENTIALS  path to Vertex service-account json
    GOOGLE_MODEL              default: gemini-2.5-flash
    GCP_LOCATION              default: asia-southeast1
    SARVAM_STT_MODEL          default: saarika:v2.5
    SARVAM_TTS_MODEL          default: bulbul:v2   (or bulbul:v3-beta)
    SARVAM_TTS_VOICE          default: anushka (v2) / aditya (v3)
    SARVAM_TTS_PACE           optional float (v2: 0.3-3.0, v3: 0.5-2.0)
    SARVAM_TTS_PITCH          optional float (v2 only, -0.75..0.75)
    SARVAM_TTS_LOUDNESS       optional float (v2 only, 0.3..3.0)
    SARVAM_TTS_TEMPERATURE    optional float (v3 only, 0.01..1.0)
    SARVAM_TTS_INR_PER_CHAR   optional override for TTS INR/char cost
"""

import asyncio
import csv
from datetime import datetime
import json
import os
import sys
import time
from urllib.parse import unquote
from fastapi import WebSocket
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    TTSSpeakFrame,
    LLMRunFrame,
    MetricsFrame,
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
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.services.google.vertex.llm import GoogleVertexLLMService
from google.genai.types import CreateCachedContentConfig
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy
from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame

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
    "pa-IN": Language.PA_IN,
}


logger.remove()
logger.add(sys.stderr, level="INFO")


class TranscriptLogger(FrameProcessor):
    """Logs STT interim + final transcripts so we can see what Sarvam heard."""
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            logger.info(f"[STT final]   {frame.text!r}")
        await self.push_frame(frame, direction)


class WhileBotSpeakingUserMuteStrategy(BaseUserMuteStrategy):
    """Mute user turn detection whenever the bot is speaking.

    Prevents phantom VAD start/stop cycles caused by bot audio bleeding
    into the user's mic (common on telephony) from corrupting the stop
    strategy's state machine. Trade-off: user cannot barge-in / interrupt
    the bot; they must wait for the bot to finish speaking.
    """

    def __init__(self):
        super().__init__()
        self._bot_speaking = False

    async def reset(self):
        self._bot_speaking = False

    async def process_frame(self, frame: Frame) -> bool:
        await super().process_frame(frame)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        return self._bot_speaking


class CustomSpeechTimeoutUserTurnStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """Fires the user turn as soon as STT emits a finalized transcript with text,
    ignoring the VAD state machine. The parent class's `_vad_stopped_time` gets
    wiped by phantom `VADUserStartedSpeakingFrame` events (echo/bleed on
    telephony), which strands the turn — this bypass avoids that entirely.
    """

    async def _handle_transcription(self, frame: TranscriptionFrame):
        await super()._handle_transcription(frame)
        # Sarvam STT only pushes TranscriptionFrame at true utterance-end and
        # never sets finalized=True, so every TranscriptionFrame is a real final.
        # Fire the turn immediately if we have text, regardless of VAD state.
        if self._text.strip():
            if self._timeout_task:
                await self.task_manager.cancel_task(self._timeout_task)
                self._timeout_task = None
            await self.trigger_user_turn_stopped()


# Removed PipelineTracer class

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

    _gen_params_logged = False

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
        else:
            params = super()._build_generation_params(
                system_instruction=system_instruction,
                tools=tools,
                tool_config=tool_config,
            )
        thinking = self._settings.thinking
        if thinking is not None and hasattr(thinking, "model_dump") and "thinking_config" not in params:
            try:
                params["thinking_config"] = thinking.model_dump(exclude_unset=True)
            except Exception:
                pass
        extra = getattr(self._settings, "extra", None) or {}
        if "stop_sequences" in extra and "stop_sequences" not in params:
            params["stop_sequences"] = extra["stop_sequences"]
        if not CachingGoogleVertexLLMService._gen_params_logged:
            CachingGoogleVertexLLMService._gen_params_logged = True
            logged = {
                k: (v if k in ("thinking_config", "stop_sequences", "max_output_tokens",
                               "temperature", "top_p", "top_k", "cached_content")
                    else f"<{type(v).__name__}>")
                for k, v in params.items()
            }
            logger.info(f"[vertex-gen-params] {logged}")
        return params


# ---------------------------------------------------------------------------
# Pricing — Sarvam STT (INR-native) + Vertex Gemini LLM (USD) + Sarvam TTS (INR)
# ---------------------------------------------------------------------------

PRICING = {
    # Sarvam STT — native INR (₹30/hr).
    "stt_sarvam_per_sec_inr":  30.0 / 3600.0,
    # Sarvam TTS — native INR per character. Override via SARVAM_TTS_INR_PER_CHAR.
    # Sarvam public list price (2026): ₹30 / 10,000 chars for Bulbul v3 realtime & streaming.
    "tts_sarvam_per_char_inr": {
        "bulbul:v2":       30.0 / 10_000.0,
        "bulbul:v3-beta":  30.0 / 10_000.0,
        "bulbul:v3":       30.0 / 10_000.0,
        "_default":        30.0 / 10_000.0,
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

    # STT — Sarvam STT (native INR).
    stt_inr = call_duration_s * PRICING["stt_sarvam_per_sec_inr"]
    stt_usd = stt_inr / inr_per_usd

    # TTS — Sarvam Bulbul (native INR per character).
    tts_model = os.getenv("SARVAM_TTS_MODEL", "bulbul:v2").lower()
    tts_rate_inr = PRICING["tts_sarvam_per_char_inr"].get(
        tts_model, PRICING["tts_sarvam_per_char_inr"]["_default"]
    )
    tts_per_char_override = os.getenv("SARVAM_TTS_INR_PER_CHAR")
    if tts_per_char_override:
        try:
            tts_rate_inr = float(tts_per_char_override)
        except ValueError:
            pass
    tts_inr = tts_characters * tts_rate_inr
    tts_usd = tts_inr / inr_per_usd

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


async def export_call_token_usage(
    websocket: WebSocket,
    metrics: "CallMetricsAccumulator",
    agent_name: str = "default_agent",
    call_duration_s: float = 0.0,
) -> None:
    """Append one row to token_logs/<date>/token_logs_<agent>_sarvam_gemini_sarvam.csv."""
    try:
        path = getattr(websocket.url, "path", "") or ""
        custom_field = unquote(path.split("/")[-1]) if path else "Unknown"

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        llm_model = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
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
        log_file = os.path.join(log_dir, f"token_logs_{agent_name}_sarvam_gemini_sarvam.csv")
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
        logger.error(f"Failed to export call token usage: {e}")


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
            max_tokens=200,
            thinking=GoogleVertexLLMService.ThinkingConfig(thinking_budget=0),
            extra={"stop_sequences": ["<start_of_turn>"]},
        ),
    )


def _build_vad() -> SileroVADAnalyzer:
    # Fixed confidence + minimum floor on start_secs to prevent phantom turn-start
    # storms when the env has an aggressive value like 0.01. Env can still raise
    # start_secs above the floor.
    confidence = 0.8
    try:
        start_secs = max(float(os.getenv("VAD_START_SECS", "0.2")), 0.15)
    except ValueError:
        start_secs = 0.2
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


def _require(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise ValueError(f"{name} env var is required")
    return v


def _require_env(name: str) -> str:
    return _require(name)


def _get_bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def _resolve_sarvam_tts(language: str) -> dict:
    """Resolve Sarvam Bulbul TTS config from env vars.

    Reads SARVAM_TTS_MODEL, SARVAM_TTS_VOICE, SARVAM_TTS_PACE,
    SARVAM_TTS_PITCH, SARVAM_TTS_LOUDNESS, SARVAM_TTS_TEMPERATURE.
    """
    model = os.getenv("SARVAM_TTS_MODEL", "bulbul:v2").lower()
    if model not in ("bulbul:v2", "bulbul:v3-beta", "bulbul:v3"):
        model = "bulbul:v2"

    default_voice = "anushka" if model == "bulbul:v2" else "aditya"
    res = {
        "model": model,
        "voice": os.getenv("SARVAM_TTS_VOICE", default_voice),
        "language": language,
    }

    def _try_float(name):
        raw = os.getenv(name)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    pace = _try_float("SARVAM_TTS_PACE")
    if pace is not None:
        res["pace"] = pace
    if model == "bulbul:v2":
        pitch = _try_float("SARVAM_TTS_PITCH")
        loudness = _try_float("SARVAM_TTS_LOUDNESS")
        if pitch is not None:
            res["pitch"] = pitch
        if loudness is not None:
            res["loudness"] = loudness
    else:
        temperature = _try_float("SARVAM_TTS_TEMPERATURE")
        if temperature is not None:
            res["temperature"] = temperature
    return res


async def run_simple_agent(
    websocket: WebSocket,
    system_instruction: str,
    dynamic_instruction: str = "",
    greeting_text: str = "",
    language: str = "hi-IN",
) -> None:
    initial_data = await websocket.receive_json()
    logger.info(f"Initial WS data: {initial_data}")
    if initial_data.get("event") != "connected":
        await websocket.close(code=1000)
        return

    # --- transport (Exotel serializer over WebSocket, 8kHz PCM) ---------
    use_local_vad = _get_bool_env("USE_LOCAL_VAD", True)
    vad = _build_vad() if use_local_vad else None
    transport = FastAPIWebsocketTransport(
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

    # --- STT: Sarvam Saarika streaming ----------------------------------
    # `vad_signals=True` is required — without it, Sarvam never emits
    # end-of-utterance signals and no transcript is ever produced.
    stt = SarvamSTTService(
        api_key=_require("SARVAM_API_KEY"),
        settings=SarvamSTTService.Settings(
            model=os.getenv("SARVAM_STT_MODEL", "saarika:v2.5"),
            language=_LANGUAGE_MAP.get(language, Language.EN_IN),
            vad_signals=True,
            high_vad_sensitivity=False,
        ),
        keepalive_timeout=10.0,
        ttfs_p99_latency=0.35,
    )

    # --- LLM: Gemini ----------------------------------
    llm = _build_google_llm(system_instruction)
    if hasattr(llm, "_ensure_cache"):
        asyncio.create_task(llm._ensure_cache())

    # --- TTS: Sarvam Bulbul (WebSocket streaming) -----------------------
    sarvam_tts_cfg = _resolve_sarvam_tts(language)
    logger.info(f"[sarvam-tts] {sarvam_tts_cfg}")
    tts_settings_kwargs = dict(
        model=sarvam_tts_cfg["model"],
        voice=sarvam_tts_cfg["voice"],
        language=sarvam_tts_cfg["language"],
        min_buffer_size=30,
    )
    for k in ("pace", "pitch", "loudness", "temperature"):
        if k in sarvam_tts_cfg:
            tts_settings_kwargs[k] = sarvam_tts_cfg[k]
    tts = SarvamTTSService(
        api_key=_require("SARVAM_API_KEY"),
        sample_rate=8000,
        settings=SarvamTTSService.Settings(**tts_settings_kwargs),
    )

    initial_messages = []
    if dynamic_instruction:
        initial_messages.append(
            {"role": "user", "content": dynamic_instruction}
        )
    context = LLMContext(messages=initial_messages)
    user_agg, asst_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=vad,
            user_idle_timeout=8.0,
            user_turn_strategies=UserTurnStrategies(
                start=[
                    VADUserTurnStartStrategy(),
                    TranscriptionUserTurnStartStrategy(use_interim=False),
                ],
                stop=[CustomSpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.1)],
            ),
            user_mute_strategies=[],
        ),
    )

    @user_agg.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy):
        logger.info(f"[aggregator] User turn started (strategy={strategy})")

    timeline = LatencyTimeline()
    tracer_stt = LatencyTracer(timeline)
    tracer_agg = LatencyTracer(timeline)
    tracer_tts = LatencyTracer(timeline)

    @user_agg.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message):
        timeline.mark("smart_turn_aggregation_complete")
        logger.info(f"[aggregator] User turn stopped: {message.content!r}")

    @user_agg.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        logger.info("[aggregator] User turn idle")

    @asst_agg.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message):
        logger.info(f"[aggregator] Assistant turn stopped: {message.content!r}")

    transcript_logger = TranscriptLogger()
    metrics_accumulator = CallMetricsAccumulator()

    # --- pipeline (no extras) -------------------------------------------
    pipeline = Pipeline([
        transport.input(),
        stt,
        transcript_logger,
        tracer_stt,
        user_agg,
        tracer_agg,
        llm,
        tts,
        tracer_tts,
        metrics_accumulator,
        transport.output(),
        asst_agg,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    )

    @transport.event_handler("on_client_connected")
    async def _on_connected(_t, _c):
        logger.info("Client connected")
        if greeting_text:
            logger.info(f"Speaking greeting: {greeting_text!r}")
            await task.queue_frame(TTSSpeakFrame(greeting_text))
        await task.queue_frame(LLMRunFrame())

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnected(_t, _c):
        logger.info("Client disconnected")
        await task.cancel()

    call_started_at = time.time()
    try:
        await PipelineRunner(handle_sigint=False).run(task)
    finally:
        call_duration_s = time.time() - call_started_at
        await export_call_token_usage(
            websocket,
            metrics_accumulator,
            agent_name="simple_sarvam_agent",
            call_duration_s=call_duration_s,
        )
