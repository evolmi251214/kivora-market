# Kivora Market Engine v16

Render-owned, stateless KVC market engine.

- One candle every **40 seconds**.
- Smooth deterministic trend/noise; all players see the same market.
- Rare 2-4% market events unfold over 8-12 candles instead of one spike.
- `/stream` uses SSE and pushes only when a candle changes.
- `/snapshot?points=60` returns about **40 minutes** of history.
- `/price` returns the current/previous candle.
- `/health` reports engine version and candle interval.
- No Supabase writes or database required.

## Render

Repository root is the service root.

Build command: `npm install`

Start command: `npm start`

Region: Singapore

Hosting `.env`:

```env
KIVORA_MARKET_URL=https://kivora-market.onrender.com
```
