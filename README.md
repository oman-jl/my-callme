# my-callme

A self-hosted clone of the **/call-me** system — lets an AI agent ring your
actual phone, read a question/update aloud, and get your spoken reply back as
text — or just text you.

This is the backend + agent tooling, mirroring the `callmemcp.com` API surface.
```
my-callme/
  server.py       FastAPI backend (the "callmemcp.com" equivalent)
  providers.py    call bridge: mock (demo/test) or twilio (real phone)
  cli.py          `callme`-compatible CLI
  mcp.mjs         MCP server so any AI agent can use ring/text/pair
  run.sh          start the backend
```

## Start

```bash
cd my-callme
./run.sh            # listens on 127.0.0.1:8800 (mock provider)
```

Default provider is **mock** — it simulates the phone so you can test the whole
chain with no external account. To make real phone calls, use the Twilio
provider (see below).

## API (mirrors callmemcp.com)

| Endpoint | Purpose |
|---|---|
| `POST /register` | register an agent sender session |
| `GET /identity` | show paired user number |
| `POST /pair` | pair a 10-digit number (rings it to prove) |
| `POST /ring` | blocking call — speak text, return spoken reply |
| `POST /text` | non-blocking SMS |
| `POST /calls` | start a call (`keep_open` keeps the line open) |
| `POST /calls/{id}/say` | speak next line on an open call |
| `POST /calls/{id}/hangup` | end an open call |
| `POST /title` | set the phone thread title |
| `GET /events` | drain events |
| `GET /listen` | long-poll for next event |

Config lives in `~/.aiphone/config.json` (provider + paired user number) and
`~/.aiphone/session.json` (agent identity).

## CLI

```bash
python3 cli.py --base http://127.0.0.1:8800 register "my agent"
python3 cli.py --base http://127.0.0.1:8800 pair 5550001234   # rings phone
python3 cli.py --base http://127.0.0.1:8800 number
python3 cli.py --base http://127.0.0.1:8800 call "Should I deploy to prod?"
python3 cli.py --base http://127.0.0.1:8800 text "Build green."
```

## MCP (for AI agents)

```js
// claude
claude mcp add --transport stdio my-callme -- node /path/to/my-callme/mcp.mjs
// codex
codex mcp add my-callme -- node /path/to/my-callme/mcp.mjs
```
Tools: `call`, `text`, `number`, `title`, `pair`. Reads pairing from
`~/.aiphone/config.json`, so you never type a number.

## Going live with Twilio (real phone calls)

The mock provider simulates the phone. For real calls, set:

```bash
export TWILIO_ACCOUNT_SID=...
export TWILIO_AUTH_TOKEN=...
export TWILIO_FROM=+15551234567        # a Twilio number you own
export MYCALLME_PUBLIC_BASE=https://your-tunnel.trycloudflare.com
./run.sh
```

The Twilio provider places a real call, uses `<Say>` (TTS) to read the text,
`<Gather input="speech">` captures the spoken reply, and Twilio's speech
recognition returns the transcript. The `public_base` must be a public HTTPS URL
(Cloudflare quick tunnel + nginx) so Twilio can reach the `/twilio/callback`
webhook.

> Note: reproducing the original /call-me *exactly* (a branded mobile app with
> CallKit + push) requires Apple/Google developer accounts and app-store
> publication, which cannot be done from this container. This Twilio gateway is
> the equivalent user-visible behavior on a real phone, fully self-hosted.
