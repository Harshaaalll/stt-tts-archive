"""Simple-agent FastAPI server.

Same /fusion-mfi-ws/{CustomField} route as the other agents, so Exotel needs
no URL changes. Runs on port 7860. Only one agent server can run at a time.

  cd /media/kabir/ssd/dev_main/stt-tts
  python simple_agent/server.py
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote

# Make parent project root importable (fusion_prompt_panch, etc.)
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

from history_retriever import init_pools, close_pools
from fusion_contextual_prompt_explore import get_dynamic_greeting, get_fusion_explore_prompt, prepare_payload
from voice_agent import run_simple_agent

_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
load_dotenv(dotenv_path=_ENV_PATH, override=True)


def get_identity_question(caller_name: str, language_name: str) -> str:
    """Build the identity confirmation question in the target language."""
    if language_name == "Hindi":
        return f"क्या मेरी बात {caller_name} जी से हो रही है?"
    elif language_name == "Marathi":
        return f"माझी बोलणे {caller_name} जी सोबत होत आहे का?"
    elif language_name == "Gujarati":
        return f"શું મારી વાત {caller_name} ભાઈ/બહેન સાથે થઈ રહી છે?"
    elif language_name == "Kannada":
        return f"ನಾನು {caller_name} ಅವರೊಂದಿಗೆ ಮಾತನಾಡುತ್ತಿದ್ದೇನೆಯೇ?"
    elif language_name == "Telugu":
        return f"నేను {caller_name} గారితో మాట్లాడుతున్నానా?"
    elif language_name == "Tamil":
        return f"நான் {caller_name} அவர்களிடம் பேசுகிறேனா?"
    elif language_name == "Bengali":
        return f"আমি কি {caller_name} বাবুর সাথে কথা বলছি?"
    elif language_name == "Punjabi":
        return f"ਕੀ ਮੈਂ {caller_name} ਜੀ ਨਾਲ ਗੱਲ ਕਰ ਰਿਹਾ ਹਾਂ?"
    elif language_name == "Odia":
        return f"ମୁଁ {caller_name} ବାବୁଙ୍କ ସହ କଥା ହେଉଛି କି?"
    else:
        return f"Am I speaking to {caller_name}?"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pools()
    try:
        yield
    finally:
        await close_pools()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.websocket("/fusion-mfi-ws/{CustomField}")
async def ws_endpoint(websocket: WebSocket, CustomField: str = None):
    await websocket.accept()
    print("[SIMPLE_AGENT] WebSocket connection accepted")
    try:
        system_instruction, dynamic_instruction = await get_fusion_explore_prompt(CustomField)
        greeting_text, greeting_lang, language_name = get_dynamic_greeting(CustomField)

        # Combine greeting with identity verification question
        try:
            payload = prepare_payload(CustomField)
            contact_type = payload.get("contact_type", "primary_contact_number")
            if contact_type == "co_applicant_number":
                caller_name = payload.get("co_applicant_name", "")
            else:
                caller_name = payload.get("customer_name", "")

            if caller_name and greeting_text:
                identity_q = get_identity_question(caller_name, language_name)
                greeting_text = f"{greeting_text} {identity_q}"
        except Exception as pe:
            print(f"[SIMPLE_AGENT] Error preparing identity greeting: {pe}")

        await run_simple_agent(
            websocket,
            system_instruction=system_instruction,
            dynamic_instruction=dynamic_instruction,
            greeting_text=greeting_text,
            language=greeting_lang or "hi-IN",
        )
    except Exception as e:
        print(f"[SIMPLE_AGENT] Exception: {e}")


@app.get("/fusion-mfi")
async def fusion_mfi_connect(request: Request) -> Dict[Any, Any]:
    print("[SIMPLE_AGENT] printing connect method args")
    print(request.url.query)
    query_params = request.url.query
    params = parse_qs(query_params)
    custom_data = params.get("CustomField", ["Unknown"])[0]
    try:
        body = await request.json()
        if isinstance(body, dict) and "system_instruction" in body:
            encoded_instruction = quote(body["system_instruction"])
            query_params = (
                f"{query_params}&system_instruction={encoded_instruction}"
                if query_params
                else f"system_instruction={encoded_instruction}"
            )
    except Exception:
        pass

    protocol = "wss" if request.url.scheme == "https" else "ws"
    ws_url = f"{protocol}://apocynaceous-nonsuccessionally-yajaira.ngrok-free.dev/fusion-mfi-ws/{custom_data}"
    print(f"[SIMPLE_AGENT] Generated WS URL for client: {ws_url}")
    return {"url": ws_url}


@app.get("/")
async def root(request: Request):
    print("[SIMPLE_AGENT] Handled root route request")
    return await fusion_mfi_connect(request)


async def main():
    port = int(os.environ.get("PORT", 7860))
    print(f"[SIMPLE_AGENT] Starting on port {port}")
    config = uvicorn.Config(app, host="0.0.0.0", port=port)
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
