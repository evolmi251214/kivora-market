'use strict';

const http = require('http');
const { URL } = require('url');

const PORT = Number(process.env.PORT || 10000);
const TICK_MS = 3000;
const MAX_POINTS = 180;
const BASE_PRICE = 100;
const MIN_PRICE = 52;
const MAX_PRICE = 158;

// Stateless deterministic market: every instance/restart derives the same price
// from the UTC tick number. No DB write is needed for every market movement.
function rand01(n) {
  let x = (Number(n) | 0) + 0x6D2B79F5;
  x = Math.imul(x ^ (x >>> 15), x | 1);
  x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
  return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function priceAt(tick) {
  const slow = 0.115 * Math.sin(tick / 183 + 0.75);
  const medium = 0.052 * Math.sin(tick / 47 + 2.1);
  const fast = 0.020 * Math.sin(tick / 9.5 + 0.35);
  const micro = (rand01(tick) - 0.5) * 0.010;

  // Rare deterministic "market event" lasting ~90 sec, not a random write.
  const eventBlock = Math.floor(tick / 120);
  const eventSeed = rand01(eventBlock * 991);
  const eventPosition = tick % 120;
  let eventMove = 0;
  if (eventSeed > 0.93 && eventPosition < 30) {
    const direction = rand01(eventBlock * 313 + 7) > 0.5 ? 1 : -1;
    const envelope = Math.sin((eventPosition / 30) * Math.PI);
    eventMove = direction * (0.035 + rand01(eventBlock * 127) * 0.035) * envelope;
  }

  return Number(clamp(BASE_PRICE * Math.exp(slow + medium + fast + micro + eventMove), MIN_PRICE, MAX_PRICE).toFixed(4));
}

function volumeAt(tick) {
  const delta = Math.abs(priceAt(tick) - priceAt(tick - 1));
  return Number((90 + rand01(tick * 17) * 760 + delta * 950).toFixed(2));
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
    tickNo: tick,
    timestamp: tick * TICK_MS,
    updatedAt: new Date(tick * TICK_MS).toISOString()
  };
}

function snapshot(points = 60) {
  points = clamp(Math.floor(Number(points) || 60), 2, MAX_POINTS);
  const now = tickNoAt();
  const ticks = [];
  for (let t = now - points + 1; t <= now; t++) {
    const p = tickPayload(t);
    ticks.push({ price: p.price, volume: p.volume, tickNo: p.tickNo, createdAt: p.updatedAt });
  }
  const current = tickPayload(now);
  return { ...current, history: ticks.map(t => t.price), ticks, tickMs: TICK_MS };
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
    return res.end(JSON.stringify({ ok: true, service: 'kivora-market', tickNo: tickNoAt(), clients: clients.size }));
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

// One server-side timer fans out to every connected player. This is the core
// optimization: market calculation happens once, not once per browser request.
setInterval(() => {
  const nowTick = tickNoAt();
  for (const client of clients) {
    if (client.lastTick === nowTick) continue;
    client.lastTick = nowTick;
    try { writeSse(client.res, 'tick', tickPayload(nowTick)); }
    catch (_) { clients.delete(client); }
  }
}, 500).unref();

setInterval(() => {
  for (const client of clients) {
    try { client.res.write(`: heartbeat ${Date.now()}\n\n`); }
    catch (_) { clients.delete(client); }
  }
}, 20000).unref();

server.listen(PORT, '0.0.0.0', () => {
  console.log(`Kivora Market Engine listening on :${PORT}`);
});
