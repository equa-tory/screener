# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup & Run

```bash
pip install -r requirements.txt
python server.py
```

Server prints the iPhone URL on start (e.g. `http://192.168.x.x:8080`). Open that in iPhone Safari.

## Architecture

Two-file project:

**`server.py`** — FastAPI WebSocket server + screen capture
- `/ws` WebSocket endpoint: sends JPEG frames as binary, receives JSON control messages `{monitor, fps, quality}`
- On connect: sends `{"type": "info", "monitors": [...]}` with resolution per monitor
- Screen capture runs in a single-thread `ThreadPoolExecutor` (mss needs COM thread affinity on Windows; `threading.local()` stores the mss context per thread)
- `_grab_frame(mon_idx, quality)` does capture → PIL JPEG encode → bytes
- Serves `static/` as HTTP on same port (index.html is the iPhone app)

**`static/index.html`** — Safari PWA, single file
- Canvas rendering: `ctx.setTransform(scale, 0, 0, scale, tx, ty)` + `drawImage(bitmap)`
- Touch via Pointer Events API: 1 finger = pan, 2 finger = pinch-zoom, double-tap = reset fit
- `createImageBitmap(blob)` for async JPEG decode (non-blocking)
- Auto-reconnect WebSocket with exponential backoff
- Monitor buttons built dynamically from server info message

## Key parameters

Defaults: 15 fps, quality 70, monitor 1. Client can change all three at runtime via WebSocket JSON.

Max zoom: 12×. Fit-to-screen is the minimum zoom (can't zoom out past fit).

## Extending

- **Outside home access**: add Tailscale on PC + phone, no code changes needed
- **Region capture**: change `sct.monitors[mon_idx]` to a custom `{"left": x, "top": y, "width": w, "height": h}` dict
- **Lower latency**: reduce quality (50-60), increase fps (20-25), or switch JPEG → WebP (`format="WEBP"` in Pillow)
