'use strict';

const http = require('http');
const { URL } = require('url');

const PORT = Number(process.env.PORT || 10000);
const TICK_MS = 40000; // one real market candle every 40 seconds
const MAX_POINTS = 120;
const BASE_PRICE = 100;
const MIN_PRICE = 58;
const MAX_PRICE = 155;

// Kivora Market v16
// -----------------
// The market remains stateless/deterministic: every Render instance derives
// the same candle from UTC time. No database write is required per candle.
// Unlike the old 3-second engine, random movement is interpolated over several
// candles so trends persist and the chart does not look like white-noise.
function rand01(n) {
  let x = (Number(n) | 0) + 0x6D2B79F5;
  x = Math.imul(x ^ (x >>> 15), x | 1);
  x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
  return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function smoothstep(t) { t = clamp(t, 0, 1); return t * t * (3 - 2 * t); }
function signedRand(seed) { return (rand01(seed) * 2) - 1; }

// Smooth deterministic noise. A value gradually moves from one seeded anchor
// to the next rather than jumping independently every candle.
function smoothNoise(tick, stride, salt) {
  const block = Math.floor(tick / stride);
  const local = (tick - block * stride) / stride;
  const a = signedRand(Math.imul(block | 0, 1103515245) + salt);
  const b = signedRand(Math.imul((block + 1) | 0, 1103515245) + salt);
  const s = smoothstep(local);
  return a + (b - a) * s;
}

function eventMoveAt(tick) {
  // Roughly one candidate event per 2 hours (180 x 40s). Only ~9% of blocks
  // actually contain an event. The whole event is a 2-4% swing spread over
  // several candles, not a single violent candle.
  const blockSize = 180;
  const block = Math.floor(tick / blockSize);
  const pos = tick - block * blockSize;
  const seed = rand01(block * 991 + 73);
  if (seed < 0.91) return 0;

  const duration = 8 + Math.floor(rand01(block * 313 + 19) * 5); // 8-12 candles
  const start = 24 + Math.floor(rand01(block * 127 + 41) * (blockSize - duration - 48));
  if (pos < start || pos > start + duration) return 0;

  const progress = (pos - start) / duration;
  const direction = rand01(block * 521 + 7) >= 0.5 ? 1 : -1;
  const amplitude = 0.020 + rand01(block * 733 + 11) * 0.020;
  return direction * amplitude * Math.sin(progress * Math.PI);
}

function rawLogLevel(tick) {
  // Slow macro cycle + medium cycle form the long market structure.
  const slow = 0.070 * Math.sin(tick / 235 + 0.75);
  const medium = 0.028 * Math.sin(tick / 68 + 2.10);

  // 10-14 candle trend pressure and 3-candle micro texture.
  const trend = 0.020 * smoothNoise(tick, 12, 0x45d9f3b);
  const micro = 0.0045 * smoothNoise(tick, 3, 0x27d4eb2d);

  return slow + medium + trend + micro + eventMoveAt(tick);
}

function priceAt(tick) {
  const price = BASE_PRICE * Math.exp(rawLogLevel(tick));
  return Number(clamp(price, MIN_PRICE, MAX_PRICE).toFixed(4));
}

function phaseAt(tick) {
  const now = priceAt(tick);
  const ago = priceAt(tick - 5); // ~3m20s trend window
  const pct = ago > 0 ? ((now - ago) / ago) * 100 : 0;
  if (pct >= 0.55) return 'bullish';
  if (pct <= -0.55) return 'bearish';
  return 'sideways';
}

function volumeAt(tick) {
  const prev = priceAt(tick - 1);
  const now = priceAt(tick);
  const pctMove = prev > 0 ? Math.abs((now - prev) / prev) * 100 : 0;
  const base = 120 + rand01(tick * 17) * 420;
  return Number((base + pctMove * 780).toFixed(2));
}

function tickNoAt(ms = Date.now()) { return Math.floor(ms / TICK_MS); }

function tickPayload(tick = tickNoAt()) {
  const price = priceAt(tick);
  const previousPrice = priceAt(tick - 1);
  return {
    symbol: 'KVC',
    price,
    previousPrice,
    volume: volumeAt(tick),
    phase: phaseAt(tick),
    tickNo: tick,
    timestamp: tick * TICK_MS,
    updatedAt: new Date(tick * TICK_MS).toISOString(),
    tickMs: TICK_MS
  };
}

function snapshot(points = 60) {
  points = clamp(Math.floor(Number(points) || 60), 2, MAX_POINTS);
  const now = tickNoAt();
  const ticks = [];
  for (let t = now - points + 1; t <= now; t++) {
    const p = tickPayload(t);
    ticks.push({
      price: p.price,
      volume: p.volume,
      phase: p.phase,
      tickNo: p.tickNo,
      createdAt: p.updatedAt
    });
  }
  const current = tickPayload(now);
  return {
    ...current,
    history: ticks.map(t => t.price),
    ticks,
    tickMs: TICK_MS,
    candleSeconds: TICK_MS / 1000,
    historyMinutes: Number(((points * TICK_MS) / 60000).toFixed(1))
  };
}

function corsHeaders(contentType = 'application/json; charset=utf-8') {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Cache-Control': 'no-store',
    'Content-Type': contentType,
    'X-Content-Type-Options': 'nosniff'
  };
}

const clients = new Set();

function writeSse(res, event, data) {
  res.write(`event: ${event}\n`);
  res.write(`data: ${JSON.stringify(data)}\n\n`);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  if (req.method === 'OPTIONS') {
    res.writeHead(204, corsHeaders());
    return res.end();
  }
  if (req.method !== 'GET') {
    res.writeHead(405, corsHeaders());
    return res.end(JSON.stringify({ error: 'Method not allowed' }));
  }

  if (url.pathname === '/health') {
    res.writeHead(200, corsHeaders());
    return res.end(JSON.stringify({
      ok: true,
      service: 'kivora-market',
      version: '16.0',
      tickMs: TICK_MS,
      tickNo: tickNoAt(),
      clients: clients.size
    }));
  }

  if (url.pathname === '/snapshot' || url.pathname === '/price') {
    const points = url.pathname === '/price' ? 2 : url.searchParams.get('points');
    res.writeHead(200, corsHeaders());
    return res.end(JSON.stringify(snapshot(points)));
  }

  if (url.pathname === '/stream') {
    res.writeHead(200, {
      ...corsHeaders('text/event-stream; charset=utf-8'),
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no'
    });
    res.write(': connected\n\n');
    const client = { res, lastTick: tickNoAt() };
    clients.add(client);
    writeSse(res, 'tick', tickPayload(client.lastTick));
    req.on('close', () => clients.delete(client));
    return;
  }

  res.writeHead(404, corsHeaders());
  res.end(JSON.stringify({ error: 'Not found' }));
});

// Lightweight boundary watcher. Price is pushed once per 40-second candle.
setInterval(() => {
  const nowTick = tickNoAt();
  for (const client of clients) {
    if (client.lastTick === nowTick) continue;
    client.lastTick = nowTick;
    try { writeSse(client.res, 'tick', tickPayload(nowTick)); }
    catch (_) { clients.delete(client); }
  }
}, 1000).unref();

// Keep long-lived SSE connections alive through proxies without generating a
// market request or a new price candle.
setInterval(() => {
  for (const client of clients) {
    try { client.res.write(`: heartbeat ${Date.now()}\n\n`); }
    catch (_) { clients.delete(client); }
  }
}, 25000).unref();

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Kivora Market Engine v16 listening on :${PORT} · candle ${TICK_MS / 1000}s`);
});
