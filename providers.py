"""
Provider abstraction for my-callme.

A "provider" is the bridge that actually reaches a physical phone. Two kinds:

  mock     - Simulates a paired phone/device inside this host. No external
             service needed. Used for demo, tests, and end-to-end verification.
  twilio  - Real phone calls + SMS via Twilio. TTS reads text aloud, the
             caller's spoken reply is transcribed (speech-to-text) and returned.
             Requires TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM.

Both expose the same interface, so the server (server.py) is provider-agnostic.
"""
from __future__ import annotations

import json
import logging
import os
import time
import threading
import urllib.parse

log = logging.getLogger("mycallme.providers")


class ProviderError(Exception):
    """Raised for a recoverable, reportable provider failure."""


class BaseProvider:
    name = "base"

    def ring(self, to: str, text: str, sender_label: str,
             keep_open: bool = False, language: str | None = None) -> dict:
        """Place a call, read `text` aloud, block for the spoken reply.
        Returns {call_id, answer, open, said_while_waiting}."""
        raise NotImplementedError

    def text(self, to: str, body: str, sender_label: str) -> dict:
        """Send a non-blocking SMS. Returns {success} or {error}."""
        raise NotImplementedError

    def say(self, call_id: str, text: str, language: str | None = None) -> dict:
        """Continue an open call: speak next line, return the reply."""
        raise NotImplementedError

    def hangup(self, call_id: str, text: str | None = None) -> dict:
        """End an open call."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# MockProvider — simulated device for demo/test
# ---------------------------------------------------------------------------

class MockProvider(BaseProvider):
    """Simulates the /call-me mobile app. A 'human' picks up, hears the text,
    and replies. Replies come from a script (per call) or a default."""

    name = "mock"
    DEFAULT_REPLY = "Yes, go ahead."

    def __init__(self, cfg: dict):
        self.answer_delay = float(cfg.get("answer_delay", 1.0))
        self.default_reply = cfg.get("reply", self.DEFAULT_REPLY)
        self._calls: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def _new_call_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"mock-{int(time.time()*1000)}-{self._seq}"

    def _reply_script(self, to: str) -> list[str]:
        # Caller may supply a scripted conversation via env for demos.
        script = os.environ.get("MOCK_REPLY_SCRIPT")
        if script:
            return [s.strip() for s in script.split("|") if s.strip()]
        return [self.default_reply]

    def ring(self, to, text, sender_label, keep_open=False, language=None):
        # Simulate connecting/ringing then answering.
        time.sleep(self.answer_delay)
        call_id = self._new_call_id()
        script = self._reply_script(to)
        first = script[0] if script else self.DEFAULT_REPLY
        if keep_open:
            with self._lock:
                self._calls[call_id] = {
                    "to": to, "script": script, "idx": 1,
                    "sender": sender_label, "open": True,
                }
            return {"call_id": call_id, "answer": first, "open": True,
                    "said_while_waiting": None}
        return {"call_id": call_id, "answer": first, "open": False,
                "said_while_waiting": None}

    def text(self, to, body, sender_label):
        log.info("mock sms -> %s: %s", to, body)
        return {"success": True}

    def say(self, call_id, text, language=None):
        with self._lock:
            call = self._calls.get(call_id)
            if not call:
                raise ProviderError("no open call with id %s" % call_id)
            script = call["script"]
            idx = call["idx"]
            if idx < len(script):
                answer = script[idx]
                call["idx"] = idx + 1
            else:
                answer = "ok"
            open_ = True  # keep open until explicit hangup, like callme
        return {"answer": answer, "open": open_, "said_while_waiting": None}

    def hangup(self, call_id, text=None):
        with self._lock:
            if call_id in self._calls:
                del self._calls[call_id]
        return {"open": False}


# ---------------------------------------------------------------------------
# TwilioProvider — real phone calls + SMS
# ---------------------------------------------------------------------------

class TwilioProvider(BaseProvider):
    """Real carrier bridge via Twilio REST API (no SDK needed)."""

    name = "twilio"
    TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts"

    def __init__(self, cfg: dict):
        self.sid = cfg.get("account_sid") or os.environ.get("TWILIO_ACCOUNT_SID")
        self.token = cfg.get("auth_token") or os.environ.get("TWILIO_AUTH_TOKEN")
        self.frm = cfg.get("from") or os.environ.get("TWILIO_FROM")
        self.public_base = cfg.get("public_base") or os.environ.get("MYCALLME_PUBLIC_BASE")
        self.verify = not cfg.get("insecure", False)
        if not (self.sid and self.token and self.frm):
            raise ProviderError(
                "Twilio provider needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN "
                "and TWILIO_FROM (a Twilio number).")
        if not self.public_base:
            log.warning("Twilio webhooks need MYCALLME_PUBLIC_BASE (public https URL)")

    def _base(self) -> str:
        return f"{self.TWILIO_API}/{self.sid}"

    def _auth(self):
        return (self.sid, self.token)

    def _req(self, method: str, path: str, data: dict | None = None):
        import requests
        url = self._base() + path
        r = requests.request(method, url, auth=self._auth(), data=data,
                             timeout=30, verify=self.verify)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise ProviderError(f"Twilio {r.status_code}: {msg}")
        return r.json()

    def _twiml_ring(self, text: str, call_id: str, keep_open: bool, language: str) -> str:
        # Speak the text, then Gather the spoken reply.
        gather = (
            '<Gather input="speech" speechTimeout="auto" timeout="5" '
            f'action="{self._cb_url(call_id)}" method="POST">'
            f'<Say language="{language}">{self._esc(text)}</Say>'
            '<Say>Go ahead.</Say></Gather>'
        )
        # If we want to keep the line open after the reply we'd add another
        # flow; for v1 we hang up after one answer and report `open:false`.
        return '<?xml version="1.0" encoding="UTF-8"?><Response>' + gather + "</Response>"

    @staticmethod
    def _esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    def _cb_url(self, call_id: str) -> str:
        return f"{self.public_base}/twilio/callback/{call_id}"

    def ring(self, to, text, sender_label, keep_open=False, language=None):
        language = language or "en"
        call_id = f"tw-{int(time.time()*1000)}"
        import urllib.parse as up
        twiml = self._twiml_ring(text, call_id, keep_open, language)
        data = {
            "To": to,
            "From": self.frm,
            "Twiml": twiml,
            "StatusCallback": f"{self.public_base}/twilio/status/{call_id}",
            "StatusCallbackEvent": "answered,completed",
        }
        self._req("POST", "/Calls.json", data)
        # For v1 we hand the caller a placeholder; the conversational answer is
        # delivered via the callback. Since /ring blocks, we set a short wait
        # for the callback payload.
        answer = self._wait_for_answer(call_id, timeout=60)
        return {"call_id": call_id, "answer": answer, "open": False,
                "said_while_waiting": None}

    def _wait_for_answer(self, call_id: str, timeout: float = 60) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with _TWILIO_ANSWERS_LOCK:
                if call_id in _TWILIO_ANSWERS:
                    return _TWILIO_ANSWERS.pop(call_id)
            time.sleep(1.0)
        raise ProviderError("no answer received (call timed out)")

    def text(self, to, body, sender_label):
        import urllib.parse as up
        self._req("POST", "/Messages.json", {
            "From": self.frm, "To": to, "Body": body,
        })
        return {"success": True}

    def say(self, call_id, text, language=None):
        raise ProviderError("conversational keep_open not yet wired for twilio")

    def hangup(self, call_id, text=None):
        return {"open": False}


# In-memory transcript store for Twilio <Gather> callback results.
_TWILIO_ANSWERS: dict[str, str] = {}
_TWILIO_ANSWERS_LOCK = threading.Lock()


def twilio_callback(call_id: str, speech_result: str):
    with _TWILIO_ANSWERS_LOCK:
        _TWILIO_ANSWERS[call_id] = speech_result


def load_provider(cfg: dict) -> BaseProvider:
    kind = (cfg.get("provider") or "mock").strip().lower()
    if kind == "twilio":
        try:
            return TwilioProvider(cfg)
        except ProviderError as e:
            # Fall back to mock so the server still boots, but log loudly.
            log.warning("Twilio unavailable, using mock: %s", e)
            return MockProvider(cfg)
    return MockProvider(cfg)
