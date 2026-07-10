# Experiment Taglines Dictionary

This file tracks the taglines for recording files and maps them to the exact optimization config.

| Tagline | Description / Exact Changes Made |
| :--- | :--- |
| **`no_vad_sg_nomute`** | **Current Experiment:** Local VAD disabled (`USE_LOCAL_VAD=false`), Gemini hosted in Singapore (`asia-southeast1`), greeting user mute strategy removed (allowing greeting barge-in), and prompt cache pre-warmed in the background. |
| **`local_vad_us`** | **Original baseline:** Silero local VAD + Turn Analyzer enabled, Gemini hosted in `us-central1`, first-speech user mute strategy active. |
| **`no_vad_us_mute`** | Local VAD disabled (`USE_LOCAL_VAD=false`), Gemini hosted in `us-central1` (US), but greeting barge-in is still muted (`FirstSpeechUserMuteStrategy` active). |
