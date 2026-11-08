#!/usr/bin/env python3
"""
my-callme — a self-hosted clone of the /call-me backend.

Mirrors the callmemcp.com API surface:
    POST /register    register an agent sender session
    GET  /identity    show the paired user number
    POST /pair        pair a phone number (rings it to prove)
    POST /ring        blocking call: speak text, return spoken reply
    POST /text        non-blocking SMS
    POST /calls       start a call (optionally keep the line open)
    POST /calls/{id}/say      speak next line on an open call
    POST /calls/{id}/hangup   end an open call
    POST /title        title the phone thread
    GET  /events       drain pending events (texts, voicemail, notices)
    GET  /listen       long-poll for the next event
    POST /twilio/callback/{id}   Twilio speech-gather webhook
    POST /twilio/status/{id}     Twilio call status webhook

Config lives in ~/.aiphone/config.json (provider + paired user number) and
~/.aiphone/session.json (agent sender identity).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from providers import load_provider, twilio_callback

log = logging.getLogger("mycallme.server")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

AIPHONE_DIR = Path(os.environ.get("AIPHONE_DIR", str(Path.home() / ".aiphone")))
CONFIG_PATH = AIPHONE_DIR / "config.json"
SESSION_PATH = AIPHONE_DIR / "session.json"

app = FastAPI(title="my-callme", version="0.1.0")

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self.config = self._load(CONFIG_PATH, {})
        self.session = self._load(SESSION_PATH, {})
        self.events: deque[dict] = deque(maxlen=200)
        self.callers: dict[str, asyncio.Queue] = {}
        self.active_calls: dict[str, dict] = {}
        self.provider = load_provider(self.config)

    @staticmethod
    def _load(path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text())
        except Exception as e:
            log.warning("bad config %s: %s", path, e)
        return default

    def save_config(self):
        AIPHONE_DIR.mkdir(mode=0o700, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.config, indent=2))
        os.chmod(CONFIG_PATH, 0o600)

    def save_session(self):
        AIPHONE_DIR.mkdir(mode=0o700, exist_ok=True)
        SESSION_PATH.write_text(json.dumps(self.session, indent=2))
        os.chmod(SESSION_PATH, 0o600)

    def pair_number(self) -> str | None:
        return self.config.get("user_number")

    def require_paired(self):
        num = self.pair_number()
        if not num:
            raise HTTPException(status_code=428, detail={
                "error": "setup_required",
                "message": "No phone is paired yet. Run POST /pair with the "
                           "10-digit number from the Call Me app (My Number).",
            })
        return num

    def push_event(self, kind: str, payload: dict):
        ev = {"kind": kind, **payload, "ts": time.time()}
        self.events.append(ev)
        # wake up /listen waiters
        for q in list(self.callers.values()):
            try:
                q.put_nowait(ev)
            except Exception:
                pass


store = Store()

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RegisterReq(BaseModel):
    label: str

class PairReq(BaseModel):
    number: str

class RingReq(BaseModel):
    to: str | None = None
    text: str
    from_: str | None = Field(None, alias="from")
    language: str | None = None

class TextReq(BaseModel):
    to: str | None = None
    body: str
    from_: str | None = Field(None, alias="from")

class CallReq(BaseModel):
    to: str | None = None
    text: str
    keep_open: bool = False
    language: str | None = None

class SayReq(BaseModel):
    session_token: str | None = None
    text: str
    language: str | None = None

class HangupReq(BaseModel):
    session_token: str | None = None
    text: str | None = None

class TitleReq(BaseModel):
    session_token: str | None = None
    title: str

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True, "provider": store.provider.name,
            "paired": bool(store.pair_number())}


@app.post("/register")
def register(req: RegisterReq):
    if not store.session.get("session_token"):
        store.session = {
            "session_number": "9" + "".join(str(uuid.uuid4().int % 10)
                                            for _ in range(9)),
            "session_token": uuid.uuid4().hex,
            "label": req.label,
        }
        store.save_session()
    return {"session_number": store.session["session_number"],
            "session_token": store.session["session_token"]}


@app.get("/identity")
def identity():
    num = store.pair_number()
    if not num:
        raise HTTPException(status_code=428, detail={
            "error": "setup_required",
            "message": "No phone paired yet.",
        })
    return {"user_number": num, "provider": store.provider.name}


@app.post("/pair")
def pair(req: PairReq):
    number = req.number.strip()
    if not (len(number) == 10 and number.isdigit()):
        raise HTTPException(status_code=400, detail={
            "error": "bad_number",
            "message": "Pair requires a 10-digit phone number.",
        })
    # Ring the phone to prove reachability (blocking).
    try:
        res = store.provider.ring(number, "Pairing complete. You can now "
                                       "receive calls and messages from your "
                                       "AI agents.", "call-me pairing")
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": str(e)})
    store.config["user_number"] = number
    store.save_config()
    return {"ok": True, "user_number": number, "proof": res.get("answer")}


@app.post("/ring")
def ring(req: RingReq):
    to = req.to or store.require_paired()
    sender = store.session.get("label", "my-callme")
    try:
        res = store.provider.ring(to, req.text, sender, keep_open=False,
                                  language=req.language or "en")
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": str(e)})
    return {"success": True, "answer": res.get("answer"),
            "call_id": res.get("call_id"), "open": False}


@app.post("/text")
def send_text(req: TextReq):
    to = req.to or store.require_paired()
    sender = store.session.get("label", "my-callme")
    try:
        res = store.provider.text(to, req.body, sender)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": str(e)})
    return {"success": True, **res}


@app.post("/calls")
def start_call(req: CallReq):
    to = req.to or store.require_paired()
    sender = store.session.get("label", "my-callme")
    try:
        res = store.provider.ring(to, req.text, sender, keep_open=req.keep_open,
                                  language=req.language or "en")
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": str(e)})
    call_id = res.get("call_id")
    store.active_calls[call_id] = {"to": to, "open": bool(res.get("open"))}
    return {"call_id": call_id, "answer": res.get("answer"),
            "open": res.get("open", False)}


@app.post("/calls/{call_id}/say")
def say(call_id: str, req: SayReq):
    if call_id not in store.active_calls:
        raise HTTPException(status_code=404, detail={"error": "no_such_call"})
    try:
        res = store.provider.say(call_id, req.text, language=req.language)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": str(e)})
    store.active_calls[call_id]["open"] = bool(res.get("open"))
    return {"answer": res.get("answer"), "open": res.get("open", True),
            "said_while_waiting": res.get("said_while_waiting")}


@app.post("/calls/{call_id}/hangup")
def hangup(call_id: str, req: HangupReq):
    if call_id in store.active_calls:
        try:
            store.provider.hangup(call_id, text=req.text)
        except Exception:
            pass
        del store.active_calls[call_id]
    return {"open": False}


@app.post("/title")
def title(req: TitleReq):
    store.config["thread_title"] = req.title
    store.save_config()
    return {"success": True}


@app.get("/events")
def events():
    out = list(store.events)
    store.events.clear()
    return {"events": out}


@app.get("/listen")
async def listen(timeout: float = 60):
    q = asyncio.Queue()
    store.callers[id(q)] = q
    try:
        # replay existing buffered events first
        if store.events:
            ev = store.events.popleft()
            return {"event": ev}
        try:
            ev = await asyncio.wait_for(q.get(), timeout=timeout)
            return {"event": ev}
        except asyncio.TimeoutError:
            return {"event": None}
    finally:
        store.callers.pop(id(q), None)


@app.post("/twilio/callback/{call_id}")
async def twilio_cb(call_id: str, request: Request):
    form = await request.form()
    speech = form.get("SpeechResult") or form.get("Digits") or ""
    twilio_callback(call_id, speech)
    # Stop Twilio from continuing.
    return Response(content='<?xml version="1.0" encoding="UTF-8"?>'
                    "<Response></Response>",
                    media_type="application/xml")


@app.post("/twilio/status/{call_id}")
async def twilio_status(call_id: str, request: Request):
    form = await request.form()
    status = form.get("CallStatus")
    if status in ("completed", "failed", "busy", "no-answer", "canceled"):
        store.push_event("call_status", {"call_id": call_id, "status": status})
    return Response(status_code=204)


if __name__ == "__main__":
    port = int(os.environ.get("MYCALLME_PORT", "8800"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
