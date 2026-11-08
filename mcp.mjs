#!/usr/bin/env node
/**
 * my-callme MCP server — exposes ring/text/pair tools to AI agents over
 * stdio JSON-RPC (Model Context Protocol). Pure Node, no dependencies.
 *
 * Talks to the local self-hosted server (default http://127.0.0.1:8800).
 * Pairing is read from ~/.aiphone/config.json like the original /call-me.
 */
import { readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const SERVER = process.env.MYCALLME_URL || 'http://127.0.0.1:8800';
const STATE_DIR = process.env.MYCALLME_STATE_DIR ||
  path.join(path.dirname(new URL(import.meta.url).pathname), '.state');
const CONFIG = path.join(STATE_DIR, 'config.json');

function pairNumber() {
  try {
    const cfg = JSON.parse(readFileSync(CONFIG, 'utf8'));
    return cfg.user_number || null;
  } catch { return null; }
}

async function api(method, p, data, timeoutMs = 120000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(SERVER + p, {
      method, signal: ctl.signal,
      headers: { 'content-type': 'application/json' },
      body: data ? JSON.stringify(data) : undefined,
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = body.detail || body;
      const msg = typeof d === 'object' ? (d.message || JSON.stringify(d)) : d;
      throw new Error(msg || `HTTP ${res.status}`);
    }
    return body;
  } finally { clearTimeout(t); }
}

const TOOLS = [
  { name: 'call', description: 'Ring the paired phone, read the text aloud, and return the spoken reply (blocking).',
    inputSchema: { type: 'object', properties: {
      text: { type: 'string', description: 'Text to speak (~2 min max).' },
      language: { type: 'string' } }, required: ['text'] } },
  { name: 'text', description: 'Send a non-blocking SMS to the paired phone.',
    inputSchema: { type: 'object', properties: {
      body: { type: 'string' } }, required: ['body'] } },
  { name: 'number', description: 'Return the paired 10-digit phone number.',
    inputSchema: { type: 'object', properties: {} } },
  { name: 'title', description: 'Set the phone thread title.',
    inputSchema: { type: 'object', properties: { title: { type: 'string' } }, required: ['title'] } },
  { name: 'pair', description: 'Pair a phone number (rings it to prove).',
    inputSchema: { type: 'object', properties: { number: { type: 'string' } }, required: ['number'] } },
];

function resultText(s) { return { content: [{ type: 'text', text: s }] }; }

async function handleCall(name, args) {
  switch (name) {
    case 'call': {
      const r = await api('POST', '/ring', { text: args.text, language: args.language });
      if (r.err) throw new Error(r.err);
      return resultText(`ANSWER: ${r.answer || ''}`);
    }
    case 'text': {
      const r = await api('POST', '/text', { body: args.body });
      return resultText(r.success ? 'sent' : 'not delivered');
    }
    case 'number': {
      const n = pairNumber();
      if (!n) throw new Error('setup_required: no phone paired. Run the pair tool first.');
      return resultText(n);
    }
    case 'title': {
      await api('POST', '/title', { title: args.title });
      return resultText('title set');
    }
    case 'pair': {
      await api('POST', '/pair', { number: args.number }, 120000);
      return resultText('paired');
    }
    default:
      throw new Error(`unknown tool: ${name}`);
  }
}

function send(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  let idx;
  while ((idx = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, idx).trim();
    buf = buf.slice(idx + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    (async () => {
      if (msg.method === 'initialize') {
        return send({ jsonrpc: '2.0', id: msg.id, result: {
          protocolVersion: '2024-11-05',
          capabilities: { tools: {} },
          serverInfo: { name: 'my-callme', version: '0.1.0' },
        } });
      }
      if (msg.method === 'notifications/initialized') return;
      if (msg.method === 'tools/list') {
        return send({ jsonrpc: '2.0', id: msg.id, result: { tools: TOOLS } });
      }
      if (msg.method === 'tools/call') {
        const name = msg.params?.name;
        const args = msg.params?.arguments || {};
        try {
          const r = await handleCall(name, args);
          return send({ jsonrpc: '2.0', id: msg.id, result: r });
        } catch (e) {
          return send({ jsonrpc: '2.0', id: msg.id,
            result: { content: [{ type: 'text', text: `error: ${e.message}` }],
                      isError: true } });
        }
      }
    })();
  }
});
