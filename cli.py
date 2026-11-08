#!/usr/bin/env python3
"""
my-callme CLI — a `callme`-compatible client for the self-hosted server.

Usage (mirrors /call-me):
    callme register "<label>"     register this agent as a sender
    callme setup                  show pairing steps
    callme qr                     show App Store QR
    callme pair <number>          pair a phone (rings it to prove)
    callme number                 print the paired number
    callme call "<text>"          blocking call: speak text, print reply
    callme text "<body>"          non-blocking SMS
    callme title "<title>"        set thread title
    callme events                 drain events
    callme listen                 long-poll for the next event
    callme status                 show server & pairing status
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

DEFAULT_BASE = os.environ.get("MYCALLME_URL", "http://127.0.0.1:8800")


def api(base, method, path, data=None, timeout=120):
    url = base.rstrip("/") + path
    try:
        r = requests.request(method, url, json=data, timeout=timeout)
    except requests.ConnectionError:
        sys.exit(f"error: cannot reach server at {url} (is it running?)")
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text}
    if r.status_code >= 400:
        err = body.get("detail", body) if isinstance(body, dict) else body
        msg = err.get("message", str(err)) if isinstance(err, dict) else err
        sys.exit(f"error ({r.status_code}): {msg}")
    return body


def cmd_status(args):
    b = api(args.base, "GET", "/health", timeout=10)
    print(json.dumps({"server": "up", "ok": b.get("ok"),
                       "provider": b.get("provider"),
                       "paired": b.get("paired")}, indent=2))


def cmd_register(args):
    body = api(args.base, "POST", "/register", {"label": args.label})
    print(f"session_number: {body['session_number']}")
    print(f"session_token:  {body['session_token']}")
    print("saved to ~/.aiphone/session.json")


def cmd_pair(args):
    print("Your phone is about to ring. Pick up and speak.")
    body = api(args.base, "POST", "/pair", {"number": args.number}, timeout=120)
    print(f"paired {body['user_number']} — proof: {body.get('proof')!r}")


def cmd_number(args):
    body = api(args.base, "GET", "/identity", timeout=10)
    print(body["user_number"])


def cmd_call(args):
    body = api(args.base, "POST", "/ring", {"text": args.text})
    print("ANSWER:", body.get("answer") or "")


def cmd_text(args):
    body = api(args.base, "POST", "/text", {"body": args.body})
    print("sent" if body.get("success") else "not delivered")


def cmd_title(args):
    api(args.base, "POST", "/title", {"title": args.title})
    print("title set")


def cmd_events(args):
    body = api(args.base, "GET", "/events", timeout=10)
    for ev in body.get("events", []):
        print(json.dumps(ev))
    if not body.get("events"):
        print("(no events)")


def cmd_listen(args):
    try:
        body = api(args.base, "GET", f"/listen?timeout={args.timeout}", timeout=300)
    except SystemExit:
        return
    if body.get("event"):
        print(json.dumps(body["event"]))
    else:
        print("(timeout, no event)")


def cmd_setup(args):
    print("To pair a phone with my-callme:")
    print("  1. Install the Call Me app on your phone (App Store / Google Play).")
    print("  2. Open the app and read your number under 'My Number'.")
    print("  3. Run:  callme pair <10-digit-number>")
    print("  4. Your phone will ring — answer and speak to confirm.")


def cmd_qr(args):
    print("QR code for the App Store link (iOS):")
    print("  https://apps.apple.com/app/call-me/id6789575165")
    print("Android: https://play.google.com/store/apps/details?id=com.serdaroztetik.callmeai")


def main():
    p = argparse.ArgumentParser(prog="callme")
    p.add_argument("--base", default=DEFAULT_BASE)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status")
    r = sub.add_parser("register"); r.add_argument("label")
    sub.add_parser("setup")
    sub.add_parser("qr")
    p2 = sub.add_parser("pair"); p2.add_argument("number")
    sub.add_parser("number")
    c = sub.add_parser("call"); c.add_argument("text")
    t = sub.add_parser("text"); t.add_argument("body")
    ti = sub.add_parser("title"); ti.add_argument("title")
    sub.add_parser("events")
    l = sub.add_parser("listen"); l.add_argument("--timeout", type=int, default=60)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 1
    {"status": cmd_status, "register": cmd_register, "pair": cmd_pair,
     "number": cmd_number, "call": cmd_call, "text": cmd_text,
     "title": cmd_title, "events": cmd_events, "listen": cmd_listen,
     "setup": cmd_setup, "qr": cmd_qr}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
