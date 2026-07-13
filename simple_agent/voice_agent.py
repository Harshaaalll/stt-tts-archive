"""Minimal Sarvam STT + Sarvam LLM + Murf TTS pipeline.

No fillers, no sanitizer, no text normalizer, no tool calls, no latency
tracer. Everything the "full" agents have is stripped out so we can
measure the raw provider-side latency floor.

Uses the same env vars as the other agents (no new names) so the shared
.env just works:
    SARVAM_API_KEY   required
    MURF_API_KEY     required
    SARVAM_STT_MODEL default: saarika:v2.5
    SARVAM_MODEL     default: sarvam-m
    MURF_VOICE_ID    plain string OR JSON dict
                     ({"voice_id":"Abhinav","style":"Conversational","model":"GEN2","rate":50,...})
    MURF_MODEL / MURF_STYLE / MURF_RATE / MURF_PITCH / MURF_VARIATION
                     optional overrides
"""

import json
import os
import sys
from fastapi import WebSocket
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    LLMRunFrame,
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
from pipecat.services.sarvam.llm import SarvamLLMService
from pipecat_murf_tts import MurfTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies

logger.remove()
logger.add(sys.stderr, level="INFO")


class TranscriptLogger(FrameProcessor):
    """Logs STT interim + final transcripts so we can see what Sarvam heard."""
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            logger.info(f"[STT final]   {frame.text!r}")
        elif isinstance(frame, InterimTranscriptionFrame):
            logger.info(f"[STT interim] {frame.text!r}")
        await self.push_frame(frame, direction)


class CustomSpeechTimeoutUserTurnStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """Custom speech timeout user turn stop strategy that avoids the race condition
    where the transcription arrives after the timeout has completed.
    """
    async def _handle_vad_user_started_speaking(self, frame):
        logger.info("[custom-strategy] VAD user started speaking")
        await super()._handle_vad_user_started_speaking(frame)

    async def _handle_vad_user_stopped_speaking(self, frame):
        logger.info(f"[custom-strategy] VAD user stopped speaking (stt_timeout={self._stt_timeout}, stop_secs={frame.stop_secs})")
        await super()._handle_vad_user_stopped_speaking(frame)

    async def _handle_transcription(self, frame: TranscriptionFrame):
        logger.info(f"[custom-strategy] Transcription received: {frame.text!r} (finalized={frame.finalized})")
        await super()._handle_transcription(frame)
        logger.info(f"[custom-strategy] After transcription: vad_stopped_time={self._vad_stopped_time}, text={self._text!r}, timeout_task={self._timeout_task}")
        if self._vad_stopped_time is not None and self._text and self._timeout_task is None:
            logger.info("[custom-strategy] Triggering turn stopped from transcription callback")
            await self.trigger_user_turn_stopped()

    async def _maybe_trigger_user_turn_stopped(self):
        logger.info(f"[custom-strategy] _maybe_trigger: vad_user_speaking={self._vad_user_speaking}, text={self._text!r}, timeout_task={self._timeout_task}, finalized={self._transcript_finalized}")
        await super()._maybe_trigger_user_turn_stopped()


class PipelineTracer(FrameProcessor):
    def __init__(self, label: str):
        super().__init__()
        self.label = label

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        from pipecat.frames.frames import AudioRawFrame
        if not isinstance(frame, AudioRawFrame):
            logger.info(f"[{self.label}] {frame}")
        await self.push_frame(frame, direction)


class DebugSarvamLLMService(SarvamLLMService):
    async def _process_context(self, context):
        try:
            openai_messages = self.get_llm_adapter()._adapt_context(context)
            logger.info(f"[debug-llm] Sending messages to Sarvam: {openai_messages}")
        except Exception as e:
            logger.info(f"[debug-llm] Failed to adapt context: {e}")
        await super()._process_context(context)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        logger.info(f"[debug-llm] Pushing frame: {frame} (direction={direction})")
        await super().push_frame(frame, direction)


def _require(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise ValueError(f"{name} env var is required")
    return v


def _resolve_murf(language: str) -> dict:
    """Mirror the other agents' Murf resolver so the same .env works.

    Reads MURF_VOICE_ID (plain string OR JSON dict with
    voice_id / style / model / rate / pitch / variation), plus overrides
    from MURF_STYLE / MURF_MODEL / MURF_RATE / MURF_PITCH / MURF_VARIATION.
    """
    res = {
        "voice_id": "en-US-natalie",
        "style": os.getenv("MURF_STYLE", "Conversational"),
        "model": os.getenv("MURF_MODEL", "FALCON").upper(),
        "rate": 0,
        "pitch": 0,
        "variation": 1,
    }

    env_voice = os.getenv("MURF_VOICE_ID")
    if env_voice:
        try:
            parsed = json.loads(env_voice)
            if isinstance(parsed, dict):
                for k in ("voice_id", "style", "model", "rate", "pitch", "variation"):
                    if k in parsed:
                        res[k] = parsed[k] if k != "model" else str(parsed[k]).upper()
            else:
                res["voice_id"] = env_voice
        except json.JSONDecodeError:
            res["voice_id"] = env_voice

    # Env-var overrides win over JSON values.
    for key, cast in (("MURF_RATE", int), ("MURF_PITCH", int), ("MURF_VARIATION", int)):
        raw = os.getenv(key)
        if raw:
            try:
                res[key.split("_")[1].lower()] = cast(raw)
            except ValueError:
                pass

    if res["model"] not in ("FALCON", "GEN2"):
        res["model"] = "FALCON"

    res["locale"] = language
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
    vad = SileroVADAnalyzer(
        sample_rate=8000,
        params=VADParams(confidence=0.8, start_secs=0.3, stop_secs=0.2),
    )
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
            language=Language.HI_IN if language.startswith("hi") else Language.EN_IN,
            vad_signals=True,
        ),
        keepalive_timeout=10.0,
        ttfs_p99_latency=0.4,
    )

    # --- LLM: Sarvam-M (India-hosted, Hindi-native) ---------------------
    llm = DebugSarvamLLMService(
        api_key=_require("SARVAM_API_KEY"),
        settings=SarvamLLMService.Settings(
            model=os.getenv("SARVAM_MODEL", "sarvam-m"),
            system_instruction=system_instruction,
            max_tokens=200,
            reasoning_effort="low",
            
        ),
    )

    # --- TTS: Murf (same env-parsing behavior as the other agents) ------
    murf = _resolve_murf(language)
    logger.info(f"[murf] {murf}")
    tts = MurfTTSService(
        api_key=_require("MURF_API_KEY"),
        params=MurfTTSService.InputParams(
            voice_id=murf["voice_id"],
            style=murf["style"],
            rate=murf["rate"],
            pitch=murf["pitch"],
            sample_rate=8000,
            format="PCM",
            model=murf["model"],
            locale=murf["locale"],
            variation=murf["variation"],
        ),
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
                stop=[CustomSpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.1)],
            ),
            user_mute_strategies=[],
        ),
    )

    @user_agg.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy):
        logger.info(f"[aggregator] User turn started (strategy={strategy})")

    @user_agg.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message):
        logger.info(f"[aggregator] User turn stopped: {message.content!r}")

    @user_agg.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        logger.info("[aggregator] User turn idle")

    @asst_agg.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message):
        logger.info(f"[aggregator] Assistant turn stopped: {message.content!r}")

    transcript_logger = TranscriptLogger()

    # --- pipeline (no extras) -------------------------------------------
    pipeline = Pipeline([
        transport.input(),
        stt,
        transcript_logger,
        user_agg,
        PipelineTracer("after-user-agg"),
        llm,
        PipelineTracer("after-llm"),
        tts,
        PipelineTracer("after-tts"),
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

    await PipelineRunner(handle_sigint=False).run(task)
