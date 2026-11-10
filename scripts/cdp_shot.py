#!/usr/bin/env python3
"""Minimal CDP screenshot helper for the GuardedBuyer demo."""
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets


async def cdp(url, out, width, height):
    profile = tempfile.mkdtemp(prefix="gb-chrome-")
    port = 9333
    proc = subprocess.Popen([
        "google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--hide-scrollbars",
        f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        f"--window-size={width},{height}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 20
        ws_url = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as r:
                    tabs = json.load(r)
                page = next((t for t in tabs if t.get("type") == "page"), None)
                if page:
                    ws_url = page["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            await asyncio.sleep(0.15)
        if not ws_url:
            raise RuntimeError("Chrome did not expose a page target")
        errors = []
        console_errors = []
        async with websockets.connect(ws_url, max_size=30_000_000) as ws:
            seq = 0

            def handle_event(raw):
                method = raw.get("method", "")
                params = raw.get("params", {})
                if method == "Runtime.exceptionThrown":
                    detail = params.get("exceptionDetails", {})
                    text = detail.get("exception", {}).get("description") or detail.get("text") or "exception"
                    errors.append(text)
                elif method == "Runtime.consoleAPICalled":
                    if params.get("type") in ("error", "assert"):
                        args = params.get("args", [])
                        console_errors.append(" ".join(str(a.get("value", a.get("description", ""))) for a in args))
                elif method == "Log.entryAdded":
                    entry = params.get("entry", {})
                    if entry.get("level") in ("error", "warning"):
                        console_errors.append(f"{entry.get('source')}: {entry.get('text')}")

            async def send(method, params=None):
                nonlocal seq
                seq += 1
                msg_id = seq
                await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                while True:
                    raw = json.loads(await ws.recv())
                    if raw.get("id") == msg_id:
                        if "error" in raw:
                            raise RuntimeError(raw["error"])
                        return raw.get("result", {})
                    handle_event(raw)

            await send("Page.enable")
            await send("Runtime.enable")
            await send("Log.enable")
            await send("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
            })
            await send("Page.navigate", {"url": url})

            ready = False
            deadline = time.time() + 30
            while time.time() < deadline:
                res = await send("Runtime.evaluate", {
                    "expression": "document.body && document.body.dataset ? document.body.dataset.ready : ''",
                    "returnByValue": True,
                })
                if res.get("result", {}).get("value") == "1":
                    ready = True
                    break
                await asyncio.sleep(0.18)
            await asyncio.sleep(0.35)
            metrics = await send("Runtime.evaluate", {
                "expression": "JSON.stringify({title: document.title, ready: document.body.dataset.ready, scrollY: window.scrollY, sh: document.body.scrollHeight, w: innerWidth, h: innerHeight})",
                "returnByValue": True,
            })
            shot = await send("Page.captureScreenshot", {"format": "png", "fromSurface": True})
            with open(out, "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            print(f"SHOT {out} ready={ready} metrics={metrics.get('result', {}).get('value')}")
            if errors:
                print("JS_EXCEPTIONS:")
                for e in errors:
                    print("  " + e.replace("\n", "\n  "))
            if console_errors:
                print("CONSOLE_ERRORS:")
                for e in console_errors:
                    print("  " + e.replace("\n", "\n  "))
            if not errors and not console_errors:
                print("NO_JS_ERRORS")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    url = sys.argv[1]
    out = sys.argv[2]
    width = int(sys.argv[3]) if len(sys.argv) > 3 else 1440
    height = int(sys.argv[4]) if len(sys.argv) > 4 else 900
    asyncio.run(cdp(url, out, width, height))
