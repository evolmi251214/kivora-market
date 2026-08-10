# Kivora Market Engine — Render

Node service without external packages. It generates the KVC market from a deterministic UTC tick, so:

- no Supabase market table writes;
- all Render instances/restarts derive the same KVC price;
- one SSE connection per player only while Trading is open;
- `/snapshot?points=60` returns initial history;
- `/stream` pushes one tick every 3 seconds;
- `/price` is used by the PHP trade server;
- `/health` is available for Render health checks.

## Render

Root directory: `render/kivora-market`
Build command: `npm install`
Start command: `npm start`
Region: Singapore

After deployment set the Kivora hosting `.env`:

```env
KIVORA_MARKET_URL=https://YOUR-SERVICE.onrender.com
```
