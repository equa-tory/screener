import asyncio
import io
import json
import os
import secrets
import socket
import ssl
import sys
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mss
from PIL import Image, ImageDraw, ImageChops, ImageStat
from fastapi import FastAPI, WebSocket, UploadFile
from fastapi.staticfiles import StaticFiles
import uvicorn

try:
    _RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:
    _RESAMPLE = Image.BILINEAR

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0
    HAS_CONTROL = True
except ImportError:
    HAS_CONTROL = False

try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False

try:
    import sounddevice as sd
    import numpy as np
    HAS_AUDIO = True
except ImportError:
    HAS_AUDIO = False

ACCESS_TOKEN: str | None = os.environ.get("SCREENER_TOKEN")
if "--auth" in sys.argv and not ACCESS_TOKEN:
    ACCESS_TOKEN = secrets.token_urlsafe(16)

PLATFORM = sys.platform  # "win32" | "darwin" | "linux"

SETTINGS_FILE = Path("settings.json")

# Windows WASAPI loopback — must use WASAPI host API device index (not MME/DirectSound)
_LOOPBACK_DEV: int | None = None
if HAS_AUDIO and PLATFORM == "win32":
    try:
        wasapi_idx = next(
            (i for i, a in enumerate(sd.query_hostapis()) if "WASAPI" in a["name"]),
            None,
        )
        if wasapi_idx is None:
            raise RuntimeError("WASAPI host API not found")
        dev_idx = sd.query_hostapis(wasapi_idx)["default_output_device"]
        if dev_idx == -1:
            raise RuntimeError("No WASAPI default output device")
        _LOOPBACK_DEV = dev_idx
        _t = sd.WasapiSettings()
        _t.loopback = True  # verify attribute exists
    except Exception as e:
        print(f"audio init: {e}")
        HAS_AUDIO = False
else:
    HAS_AUDIO = False

def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}

def _save_settings(s: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(s))
    except Exception:
        pass

_saved = _load_settings()

from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(app):
    loop = asyncio.get_event_loop()
    def _exc_handler(loop, ctx):
        exc = ctx.get("exception")
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, OSError)):
            return
        loop.default_exception_handler(ctx)
    loop.set_exception_handler(_exc_handler)
    yield

app = FastAPI(lifespan=_lifespan)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mss")
_sct_local = threading.local()


def get_local_ip() -> str:
    # Connect to external IP so routing picks the real LAN adapter, not WSL/VPN virtual NICs
    for target in (("8.8.8.8", 80), ("1.1.1.1", 80), ("10.255.255.255", 1)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(target)
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass
    return "127.0.0.1"


def _draw_cursor(img: Image.Image, x: int, y: int) -> None:
    d = ImageDraw.Draw(img)
    # Black outline ring
    d.ellipse([x - 10, y - 10, x + 10, y + 10], fill=(0, 0, 0))
    # White body
    d.ellipse([x - 8, y - 8, x + 8, y + 8], fill=(255, 255, 255))
    # Blue center dot
    d.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(0, 140, 255))
    # Crosshair
    d.line([x - 13, y, x - 10, y], fill=(0, 0, 0), width=2)
    d.line([x + 10, y, x + 13, y], fill=(0, 0, 0), width=2)
    d.line([x, y - 13, x, y - 10], fill=(0, 0, 0), width=2)
    d.line([x, y + 10, x, y + 13], fill=(0, 0, 0), width=2)


def _grab_small(mon_idx: int) -> Image.Image:
    """Grab a downscaled frame for motion comparison (cheap)."""
    if not hasattr(_sct_local, "sct"):
        _sct_local.sct = mss.MSS()
    sct = _sct_local.sct
    mon = sct.monitors[mon_idx]
    shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img.resize((160, 90), _RESAMPLE)


def _grab_frame(mon_idx: int, quality: int) -> bytes:
    if not hasattr(_sct_local, "sct"):
        _sct_local.sct = mss.MSS()
    sct = _sct_local.sct
    mon = sct.monitors[mon_idx]
    shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    if HAS_CONTROL:
        try:
            cx, cy = pyautogui.position()
            px, py = cx - mon["left"], cy - mon["top"]
            if 0 <= px < mon["width"] and 0 <= py < mon["height"]:
                _draw_cursor(img, px, py)
        except Exception:
            pass

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def generate_icons() -> None:
    for size in (180, 192, 512):
        path = Path(f"static/icon-{size}.png")
        if path.exists():
            continue
        img = Image.new("RGB", (size, size), (8, 8, 14))
        d = ImageDraw.Draw(img)
        p = size // 10
        # Monitor outline
        d.rounded_rectangle(
            [p, p, size - p, int(size * 0.68)],
            radius=size // 15,
            outline=(0, 150, 230),
            width=max(3, size // 40),
        )
        # Screen glow
        d.rounded_rectangle(
            [p * 2, p * 2, size - p * 2, int(size * 0.60)],
            radius=size // 20,
            fill=(0, 35, 70),
        )
        # Stand
        cx = size // 2
        sw = size // 10
        d.rectangle([cx - sw, int(size * 0.68), cx + sw, int(size * 0.82)], fill=(0, 110, 180))
        d.rectangle([cx - sw * 2, int(size * 0.82), cx + sw * 2, int(size * 0.88)], fill=(0, 110, 180))
        img.save(path)


@app.websocket("/ws")
async def stream(ws: WebSocket):
    if ACCESS_TOKEN:
        if ws.query_params.get("token") != ACCESS_TOKEN:
            await ws.close(code=4401)
            return

    await ws.accept()

    with mss.MSS() as sct:
        raw_mons = sct.monitors[1:]

    mon_rects = {i + 1: dict(m) for i, m in enumerate(raw_mons)}
    mons_info = [
        {"index": i + 1, "width": m["width"], "height": m["height"]}
        for i, m in enumerate(raw_mons)
    ]
    state = {
        "monitor":         _saved.get("monitor", 1),
        "fps":             _saved.get("fps", 15),
        "quality":         _saved.get("quality", 70),
        "mouseMode":       _saved.get("mouseMode", "absolute"),
        "sensitivity":     _saved.get("sensitivity", 2.5),
        "scrollSpeed":     _saved.get("scrollSpeed", 3),
        "motionDetect":    _saved.get("motionDetect", "off"),
        "motionThreshold": _saved.get("motionThreshold", 8),
    }
    await ws.send_text(json.dumps({
        "type": "info",
        "monitors": mons_info,
        "has_control": HAS_CONTROL,
        "has_audio": HAS_AUDIO,
        "platform": PLATFORM,
        "settings": dict(state),
    }))
    loop = asyncio.get_event_loop()

    async def send_frames():
        while True:
            frame = await loop.run_in_executor(
                _executor, _grab_frame, state["monitor"], state["quality"]
            )
            await ws.send_bytes(frame)
            await asyncio.sleep(1.0 / state["fps"])

    async def recv_cmds():
        async for raw in ws.iter_text():
            try:
                data = json.loads(raw)
                t = data.get("type", "settings")

                if t == "settings":
                    for k in ("monitor", "fps", "quality", "scrollSpeed", "motionThreshold"):
                        if k in data:
                            state[k] = int(data[k])
                    if "sensitivity" in data:
                        state["sensitivity"] = float(data["sensitivity"])
                    if "mouseMode" in data:
                        state["mouseMode"] = str(data["mouseMode"])
                    if "motionDetect" in data:
                        state["motionDetect"] = str(data["motionDetect"])
                    _saved.update(state)
                    _save_settings(_saved)

                elif t == "mouse" and HAS_CONTROL:
                    rect = mon_rects.get(state["monitor"], mon_rects[1])
                    action = data.get("action", "move")
                    mods = data.get("modifiers", [])

                    if action in ("move", "click", "rclick"):
                        abs_x = int(rect["left"] + float(data["x"]) * rect["width"])
                        abs_y = int(rect["top"]  + float(data["y"]) * rect["height"])
                        if action == "move":
                            pyautogui.moveTo(abs_x, abs_y, _pause=False)
                        else:
                            btn = "right" if action == "rclick" else "left"
                            for m in mods:
                                pyautogui.keyDown(m)
                            pyautogui.click(abs_x, abs_y, button=btn, _pause=False)
                            for m in reversed(mods):
                                pyautogui.keyUp(m)

                    elif action == "move_rel":
                        dx, dy = int(data.get("dx", 0)), int(data.get("dy", 0))
                        cur = pyautogui.position()
                        sw, sh = pyautogui.size()
                        pyautogui.moveTo(
                            max(0, min(sw - 1, cur.x + dx)),
                            max(0, min(sh - 1, cur.y + dy)),
                            _pause=False,
                        )

                    elif action == "scroll":
                        abs_x = int(rect["left"] + float(data.get("x", 0.5)) * rect["width"])
                        abs_y = int(rect["top"]  + float(data.get("y", 0.5)) * rect["height"])
                        pyautogui.scroll(int(data.get("dy", 0)), x=abs_x, y=abs_y)

                elif t == "key" and HAS_CONTROL:
                    key = data.get("key", "")
                    if key:
                        pyautogui.press(key)

                elif t == "hotkey" and HAS_CONTROL:
                    keys = data.get("keys", [])
                    if keys:
                        pyautogui.hotkey(*keys)

                elif t == "type" and HAS_CONTROL:
                    text = data.get("text", "")
                    if text:
                        # ASCII only — use paste for unicode
                        safe = "".join(c for c in text if ord(c) < 128)
                        if safe:
                            pyautogui.typewrite(safe, interval=0.02)

                elif t == "paste" and HAS_CONTROL and HAS_CLIPBOARD:
                    text = data.get("text", "")
                    if text:
                        pyperclip.copy(text)
                        if PLATFORM == "darwin":
                            pyautogui.hotkey("command", "v")
                        else:
                            pyautogui.hotkey("ctrl", "v")

            except Exception as e:
                print(f"recv: {e}")

    async def motion_detect():
        last_small: Image.Image | None = None
        last_pos: tuple | None = None
        cooldown_until = 0.0

        while True:
            await asyncio.sleep(1.0)
            mode = state.get("motionDetect", "off")
            if mode == "off":
                last_small = None
                last_pos = None
                continue

            now = loop.time()
            if now < cooldown_until:
                continue

            triggered = None

            if mode in ("input", "both") and HAS_CONTROL:
                try:
                    pos = pyautogui.position()
                    cur = (pos.x, pos.y)
                    if last_pos and (abs(cur[0] - last_pos[0]) > 10 or abs(cur[1] - last_pos[1]) > 10):
                        triggered = "input"
                    last_pos = cur
                except Exception:
                    pass

            if triggered is None and mode in ("screen", "both"):
                try:
                    small = await loop.run_in_executor(_executor, _grab_small, state["monitor"])
                    if last_small is not None:
                        diff = ImageChops.difference(last_small, small)
                        stat = ImageStat.Stat(diff)
                        mean_diff = sum(stat.mean) / len(stat.mean)
                        if mean_diff > state.get("motionThreshold", 8):
                            triggered = "screen"
                    last_small = small
                except Exception:
                    pass

            if triggered:
                try:
                    await ws.send_text(json.dumps({"type": "motion", "source": triggered}))
                except Exception:
                    pass
                cooldown_until = now + 5.0

    send_task   = asyncio.create_task(send_frames())
    recv_task   = asyncio.create_task(recv_cmds())
    motion_task = asyncio.create_task(motion_detect())
    try:
        await asyncio.wait([send_task, recv_task, motion_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        send_task.cancel()
        recv_task.cancel()
        motion_task.cancel()
        await asyncio.gather(send_task, recv_task, motion_task, return_exceptions=True)


_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.heic', '.heif', '.tiff', '.tif'}

@app.post("/upload")
async def upload_file(file: UploadFile):
    desktop = Path.home() / "Desktop"
    desktop.mkdir(exist_ok=True)
    dest = desktop / (file.filename or "upload")
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while dest.exists():
        dest = desktop / f"{stem}_{n}{suffix}"
        n += 1
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    is_image = (
        suffix.lower() in _IMAGE_EXTS
        or (file.content_type or "").startswith("image/")
    )
    in_clipboard = False

    if is_image:
        if PLATFORM == "win32":
            try:
                ps = (
                    "Add-Type -Assembly System.Windows.Forms;"
                    "[System.Windows.Forms.Clipboard]::SetImage("
                    f"[System.Drawing.Image]::FromFile('{dest}'))"
                )
                subprocess.run(["powershell", "-Command", ps],
                               capture_output=True, timeout=8)
                in_clipboard = True
            except Exception:
                pass
        elif PLATFORM == "darwin":
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f'set the clipboard to (read (POSIX file "{dest}") as TIFF picture)'],
                    capture_output=True, timeout=8,
                )
                in_clipboard = True
            except Exception:
                pass

    if not in_clipboard:
        try:
            if PLATFORM == "win32":
                os.startfile(dest)
            elif PLATFORM == "darwin":
                subprocess.run(["open", str(dest)])
            else:
                subprocess.run(["xdg-open", str(dest)])
        except Exception:
            pass

    return {"saved": dest.name, "in_clipboard": in_clipboard}


@app.websocket("/audio")
async def audio_stream(ws: WebSocket):
    if not HAS_AUDIO:
        await ws.close(code=4000)
        return
    if ACCESS_TOKEN:
        if ws.query_params.get("token") != ACCESS_TOKEN:
            await ws.close(code=4401)
            return
    await ws.accept()

    RATE, CHUNK = 22050, 2048
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=12)

    def _cb(indata, frames, t, status):
        mono = indata.mean(axis=1).astype("float32").tobytes()
        def _enq():
            if not queue.full():
                queue.put_nowait(mono)
        loop.call_soon_threadsafe(_enq)

    try:
        _ws = sd.WasapiSettings()
        _ws.loopback = True
        stream = sd.InputStream(
            samplerate=RATE, channels=2, dtype="float32",
            device=_LOOPBACK_DEV, blocksize=CHUNK, callback=_cb,
            extra_settings=_ws,
        )
        stream.start()
        try:
            while True:
                chunk = await queue.get()
                await ws.send_bytes(chunk)
        finally:
            stream.stop()
            stream.close()
    except Exception as e:
        print(f"audio: {e}")


@app.get("/screener.crt")
async def serve_cert():
    from fastapi.responses import FileResponse
    p = Path("screener.crt")
    if p.exists():
        return FileResponse(str(p), media_type="application/x-x509-ca-cert")
    from fastapi.responses import Response
    return Response(status_code=404)

app.mount("/", StaticFiles(directory="static", html=True), name="static")

def _cert_has_ip(cert_path: Path, ip: str) -> bool:
    """Return True if cert has ip in SAN and is a CA cert."""
    try:
        import ipaddress as _ipa
        from cryptography import x509 as _cx509
        cert = _cx509.load_pem_x509_certificate(cert_path.read_bytes())
        bc = cert.extensions.get_extension_for_class(_cx509.BasicConstraints)
        if not bc.value.ca:
            return False
        san = cert.extensions.get_extension_for_class(_cx509.SubjectAlternativeName)
        return _ipa.IPv4Address(ip) in san.value.get_values_for_type(_cx509.IPAddress)
    except Exception:
        return False


def _make_ssl_cert(ip: str) -> tuple[str, str]:
    """Generate a CA cert for --ssl mode with SAN for ip. Returns (cert_path, key_path)."""
    cert_path = Path("screener.crt")
    key_path  = Path("screener.key")

    # Reuse existing cert only if it already covers the current IP and is a CA cert
    if cert_path.exists() and key_path.exists() and _cert_has_ip(cert_path, ip):
        return str(cert_path), str(key_path)

    # Delete stale cert
    cert_path.unlink(missing_ok=True)
    key_path.unlink(missing_ok=True)

    try:
        import datetime, ipaddress
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Screener CA")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(priv.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.IPv4Address(ip)),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]), critical=False)
            .sign(priv, hashes.SHA256())
        )
        key_path.write_bytes(priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    except ImportError:
        print("SSL cert generation requires: pip install cryptography")
        sys.exit(1)
    except Exception as e:
        print(f"SSL cert generation failed: {e}")
        sys.exit(1)

    return str(cert_path), str(key_path)


if __name__ == "__main__":
    generate_icons()
    ip = get_local_ip()
    use_ssl = "--ssl" in sys.argv
    with mss.MSS() as sct:
        mons = sct.monitors[1:]
    print(f"\n  Platform : {PLATFORM}")
    print(f"  Monitors : {len(mons)}")
    for i, m in enumerate(mons):
        print(f"    {i+1}: {m['width']}x{m['height']} @ ({m['left']},{m['top']})")
    print(f"  Control  : {'yes (pyautogui)' if HAS_CONTROL else 'no  — pip install pyautogui'}")
    proto = "https" if use_ssl else "http"
    if ACCESS_TOKEN:
        print(f"\n  iPhone URL : {proto}://{ip}:8080/?token={ACCESS_TOKEN}")
    else:
        print(f"\n  iPhone URL : {proto}://{ip}:8080")
        print("  (run with --auth to require a token)")
    if use_ssl:
        cert, key = _make_ssl_cert(ip)
        print(f"\n  HTTPS — to trust on iPhone (one-time):")
        print(f"    1. Safari → https://{ip}:8080/screener.crt → Allow download")
        print(f"    2. Settings → General → VPN & Device Management → Screener CA → Install")
        print(f"    3. Settings → General → About → Certificate Trust Settings → Screener CA → Enable")
        print()
        uvicorn.run(app, host="0.0.0.0", port=8080, log_level="error",
                    ws_ping_interval=None, ssl_certfile=cert, ssl_keyfile=key)
    else:
        print()
        uvicorn.run(app, host="0.0.0.0", port=8080, log_level="error", ws_ping_interval=None)
