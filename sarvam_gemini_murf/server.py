"""
Sarvam+Gemini+Murf FastAPI server — drop-in alternative to remote_server.py /
sarvam_agent/server.py. Same /fusion-mfi-ws/{CustomField} endpoint shape so
Exotel does not need URL changes; runs on port 7860 by default.

  cd /media/kabir/ssd/local-stt-tts-rnd/llm-tts
  python sarvam_gemini_murf/server.py

Run EITHER this server OR another — they all share port 7860.
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote

# Make the parent project root importable (fusion_prompt_panch, history_retriever,
# backchannel all live there). Use sys.path.append (not insert) so local
# modules inside sarvam_gemini_murf/ still resolve first.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent import run_agent_live_fintech_exotel
from fusion_prompt_panch import get_dynamic_greeting, get_fusion_negotiation_prompt

_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
load_dotenv(dotenv_path=_ENV_PATH, override=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.websocket("/fusion-mfi-ws/{CustomField}")
async def websocket_endpoint(
        websocket: WebSocket,
        voice: Optional[str] = None,
        language: str = "en-US",
        CustomField: str = None,
):
    await websocket.accept()
    print("[SARVAM+GEMINI+MURF] WebSocket connection accepted")
    try:
        print(websocket.query_params)
        system_instruction, dynamic_instruction, _ = await get_fusion_negotiation_prompt(CustomField)
        print(CustomField)

        greeting_text, greeting_lang, _ = get_dynamic_greeting(CustomField)

        await run_agent_live_fintech_exotel(
            websocket,
            voice=voice,
            language=greeting_lang or language,
            greeting_text=greeting_text,
            system_instruction=system_instruction,
            dynamic_instruction=dynamic_instruction,
        )
    except Exception as e:
        print(f"[SARVAM+GEMINI+MURF] Exception in run_bot: {e}")


@app.get("/fusion-mfi")
async def fusion_mfi_connect(request: Request) -> Dict[Any, Any]:
    print("[SARVAM+GEMINI+MURF] printing connect method args")
    print(request.url.query)
    query_params = request.url.query
    params = parse_qs(query_params)
    custom_data = params.get('CustomField')[0]
    try:
        body = await request.json()
        if isinstance(body, dict):
            if "system_instruction" in body:
                encoded_instruction = quote(body["system_instruction"])
                if query_params:
                    query_params += f"&system_instruction={encoded_instruction}"
                else:
                    query_params = f"system_instruction={encoded_instruction}"
    except Exception:
        pass

    is_production = False
    protocol = "wss" if request.url.scheme == "https" else "ws"
    if is_production:
        ws_url = f"ws://{request.url.hostname}/voicebot/ws?{query_params}"
    else:
        ws_url = f"{protocol}://apocynaceous-nonsuccessionally-yajaira.ngrok-free.dev/fusion-mfi-ws/{custom_data}"

    print(f"[SARVAM+GEMINI+MURF] Generated WS URL for client: {ws_url}")
    return {"url": ws_url}


@app.get("/")
async def root_bot_connect(request: Request):
    print("[SARVAM+GEMINI+MURF] Handled root route request")
    return await fusion_mfi_connect(request)


_CLIENT_DIST = os.path.join(_PROJECT_ROOT, "client", "dist")
if os.path.exists(_CLIENT_DIST):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_CLIENT_DIST, "assets")),
        name="assets",
    )

    @app.get("/{catch_all:path}")
    async def read_index(catch_all: str):
        return FileResponse(os.path.join(_CLIENT_DIST, "index.html"))


async def main():
    port = int(os.environ.get("PORT", 7860))
    print(f"[SARVAM+GEMINI+MURF] Starting on port {port}")
    config = uvicorn.Config(app, host="0.0.0.0", port=port)
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
