#!/usr/bin/env python3
"""Build the demo video automatically: headless Chrome drives the real web demo, edge-tts narrates, ffmpeg assembles.

    python3 scripts/video/make_demo_video.py [--out materials] [--voice en-US-AndrewNeural] [--port 8031]

Output: <out>/demo_video.mp4 (burned-in English captions), <out>/demo_video.srt, <out>/demo_video_narration.txt.
No keys are used; the demo server runs the offline PayPal emulation unless you pass --sandbox (needs .secrets).
"""
import argparse, asyncio, hashlib, re, base64, json, os, shutil, signal, subprocess, sys, tempfile, time, urllib.request, wave
from html import escape

import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
W, H = 1600, 900
FPS_CAPTURE = 12

# ---------------------------------------------------------------- narration (only numbers from docs/comparison_200.md)
SCENES = [
    ("title", [
        "AI agents can now shop and pay on behalf of a business.",
        "But one line hidden in a supplier listing can hijack them. And when a delivery comes up short, nobody claims the money back.",
    ]),
    ("architecture", [
        "Here is the shape of it: the agent proposes, a guardrail layer decides, PayPal moves the money, and every step is hash-chained.",
    ]),
    ("dashboard", [
        "This is GuardedBuyer, a buyer-side purchasing agent built on PayPal and AP2 mandates.",
        "An LLM reads the request and compares supplier quotes. Deterministic code decides whether any money moves.",
    ]),
    ("desk_blocked", [
        "An employee asks for two laptops: twenty-two hundred dollars, against fifteen hundred left in the IT budget.",
        "The agent tries to pay anyway. The payment is refused and logged, because the agent has no approve tool at all.",
        "The approver sees why it was blocked, the chosen quote, and the exact limits that will be signed.",
    ]),
    ("desk_release", [
        "The manager approves with a note, then finance approves.",
        "Release signs a fresh AP2 Intent Mandate that pins the merchant, item, quantity and maximum amount, and verifies it before PayPal is called.",
    ]),
    ("paypal", [
        "Release creates a real order on the PayPal sandbox through Orders v2. That is its sandbox order ID.",
        "A sandbox buyer would approve it on PayPal. Here, PayPal's sandbox test card stands in for that click.",
        "The agent reads the order back, checks it against the mandate, and only then captures. The capture ID comes straight from PayPal.",
    ]),
    ("mismatch", [
        "Back in the offline emulation, four black toner cartridges are paid for, but only three arrive.",
        "The agent reconciles the delivery and drafts a PayPal dispute for exactly the missing amount, with a factual message.",
        "Buyers can't open disputes through the API, so a person files the draft, and the agent tracks it through the Disputes API.",
    ]),
    ("tamper", [
        "And if anyone edits a single audit entry, verification breaks at exactly that record.",
    ]),
    ("red_team", [
        "Next, a poisoned listing with a fake system notice, and a fully compromised agent that obeys it.",
        "It tries to approve itself, switch to the attacker's store, and order four hundred units. Every attempt is blocked, and the real order still goes through.",
    ]),
    ("evaluation", [
        "We measured this on two hundred synthetic requests, each run three ways.",
        "Full guardrails stopped ninety-one of ninety-six attack cases. A classic spend policy stopped sixty. Neither blocked a single legitimate purchase.",
        "The five misses are listed openly, one click away.",
    ]),
    ("terminal", [
        "The same client passes a smoke test against the real PayPal sandbox: idempotent create, the not-approved path, capture, and a safe second capture.",
    ]),
    ("close", [
        "GuardedBuyer. Let AI do the buying, without handing it the keys.",
    ]),
]
GAP = 0.35          # silence between sentences
SCENE_TAIL = 0.6    # silence after the last sentence of a scene


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path]).stdout
    return float(out.strip())


# ---------------------------------------------------------------- TTS
async def tts_all(work, voice, rate):
    import edge_tts
    plan = []
    for si, (name, sents) in enumerate(SCENES):
        items = []
        for k, text in enumerate(sents):
            key = hashlib.sha1(f"{voice}|{rate}|{text}".encode()).hexdigest()[:12]
            mp3 = os.path.join(work, f"s{si:02d}_{k}_{key}.mp3"); wav = mp3[:-4] + ".wav"
            min_dur = 0.04 * len(text)          # ~25 chars/s is already fast speech; anything shorter is truncated
            if os.path.exists(wav) and duration(wav) < min_dur: os.remove(wav)
            if not os.path.exists(wav):
                for attempt in range(8):
                    try:
                        await edge_tts.Communicate(text, voice, rate=rate).save(mp3)
                        run(["ffmpeg", "-y", "-v", "error", "-i", mp3, "-ar", "48000", "-ac", "1", wav])
                        if duration(wav) >= min_dur: break
                        os.remove(wav); raise RuntimeError(f"truncated TTS for: {text[:50]}")
                    except Exception:
                        if attempt == 7: raise
                        await asyncio.sleep(3 + attempt * 3)
            items.append({"text": text, "wav": wav, "dur": duration(wav)})
        t = 0.6 if name not in ("title",) else 0.8
        for it in items:
            it["start"] = t; t += it["dur"] + GAP
        plan.append({"name": name, "items": items, "length": t - GAP + SCENE_TAIL})
    return plan


def build_audio(plan, work):
    """One mono 48 kHz WAV with every sentence at its planned offset."""
    total = sum(s["length"] for s in plan)
    n = int(total * 48000) + 48000
    buf = bytearray(n * 2)
    off = 0.0
    for s in plan:
        for it in s["items"]:
            with wave.open(it["wav"]) as w:
                data = w.readframes(w.getnframes())
            pos = int((off + it["start"]) * 48000) * 2
            buf[pos:pos + len(data)] = data
        off += s["length"]
    out = os.path.join(work, "narration.wav")
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000); w.writeframes(bytes(buf[:int(total * 48000) * 2]))
    return out, total


def synth_pad(seconds, sr=48000, seed=20261110):
    """Our own ambient pad (no samples, no third-party music): slow detuned-sine chords with soft attack/release.
    Returns float32 mono in [-1, 1], normalised to peak 1."""
    import numpy as np
    rng = np.random.default_rng(seed)
    n = int(seconds * sr); t = np.arange(n) / sr
    hz = lambda m: 440.0 * 2 ** ((m - 69) / 12)
    chords = [[48, 55, 64, 71, 74], [45, 52, 60, 67, 71], [41, 48, 57, 64, 67], [43, 50, 59, 62, 69]]   # Cmaj9 Am7 Fmaj7 G6
    seg = 9.0; out = np.zeros(n, dtype=np.float64)
    for k in range(int(seconds // seg) + 2):
        a, b = int(max(0, (k * seg - 2.5)) * sr), int(min(seconds, (k + 1) * seg + 2.5) * sr)
        if a >= n or b <= a: continue
        tt = t[a:b]; L = b - a
        env = np.minimum(1, np.minimum(np.arange(L) / (2.5 * sr), (L - np.arange(L)) / (2.5 * sr)))
        env = np.sin(env * np.pi / 2) ** 2
        for m in chords[k % len(chords)]:
            f = hz(m); ph = rng.uniform(0, 2 * np.pi, 3)
            v = (np.sin(2 * np.pi * f * tt + ph[0]) + 0.7 * np.sin(2 * np.pi * (f + 0.18) * tt + ph[1])
                 + 0.35 * np.sin(2 * np.pi * 2 * f * tt + ph[2]) * 0.3)
            out[a:b] += v * env * (0.8 if m < 50 else 0.5)
    out *= 0.85 + 0.15 * np.sin(2 * np.pi * 0.07 * t)          # slow swell
    fade = np.minimum(1, np.minimum(t / 2.0, (seconds - t) / 3.0)); out *= np.clip(fade, 0, 1)
    return (out / (np.abs(out).max() + 1e-9)).astype(np.float32)


def mix_music(narration_wav, out_wav, pad_db=-28.0, duck_db=-8.0):
    """Mix the synthesized pad under the narration: pad peaks at `pad_db` dBFS and ducks by `duck_db` while someone speaks."""
    import numpy as np
    with wave.open(narration_wav) as w:
        sr = w.getframerate(); voice = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    pad = synth_pad(len(voice) / sr, sr)[:len(voice)]
    win = int(0.05 * sr); e = np.sqrt(np.convolve(voice ** 2, np.ones(win) / win, mode="same"))
    speaking = (e > 0.01).astype(np.float32)
    k = np.ones(int(0.6 * sr)) / int(0.6 * sr); speaking = np.clip(np.convolve(speaking, k, mode="same") * 2, 0, 1)   # smooth on/off
    gain = 10 ** (pad_db / 20) * 10 ** (duck_db * speaking / 20)
    mix = np.clip(voice + pad * gain, -1, 1)
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes((mix * 32767).astype(np.int16).tobytes())
    return out_wav


def srt_time(t):
    ms = int(round(t * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_caption(text, limit=78):
    """Split a sentence into caption chunks of at most `limit` characters at word boundaries."""
    words, chunks, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > limit:
            chunks.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur: chunks.append(cur)
    return chunks


def build_srt(plan, path):
    lines, idx, off = [], 1, 0.0
    for s in plan:
        for it in s["items"]:
            chunks = split_caption(it["text"])
            total_chars = sum(len(c) for c in chunks)
            t = off + it["start"]
            for c in chunks:
                d = it["dur"] * len(c) / total_chars
                lines += [str(idx), f"{srt_time(t)} --> {srt_time(t + d - 0.02)}", c, ""]
                idx += 1; t += d
        off += s["length"]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------- static cards (title / terminal / close)
CARD_CSS = """body{margin:0;width:100vw;height:100vh;display:flex;align-items:center;justify-content:center;
background:#0b1f3f;color:#fff;font-family:'Noto Sans',Arial,sans-serif}
.wrap{width:1180px}.k{font-size:20px;letter-spacing:.14em;text-transform:uppercase;color:#9cc7ff;margin-bottom:26px}
.l{font-size:50px;font-weight:700;line-height:1.22;margin:0 0 22px;opacity:0;transform:translateY(10px);transition:all .6s ease}
.l.on{opacity:1;transform:none}.l .r{color:#ff8f8f}.s{font-size:24px;color:#c9d8ee;margin-top:30px}
.brand{display:flex;align-items:center;gap:18px;margin-bottom:34px}.mark{width:64px;height:64px;border-radius:16px;background:#0070e0;
display:flex;align-items:center;justify-content:center}.chips{display:flex;gap:12px;flex-wrap:wrap;margin-top:28px}
.chip{border:1px solid #3a5f95;border-radius:999px;padding:8px 16px;font-size:20px;color:#dbe8fb}"""
SHIELD = ("<svg width='36' height='36' viewBox='0 0 24 24' fill='none' stroke='#fff' stroke-width='2' stroke-linecap='round' "
          "stroke-linejoin='round'><path d='M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 "
          "6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z'/><path d='m9 12 2 2 4-4'/></svg>")


BOT_ICON = ("<symbol id='lucide-bot' viewBox='0 0 24 24'><path d='M12 8V4H8'/><rect width='16' height='12' x='4' y='8' rx='2'/>"
            "<path d='M2 14h2'/><path d='M20 14h2'/><path d='M15 13v2'/><path d='M9 13v2'/></symbol>")
ARCH_CSS = """*{box-sizing:border-box}body{margin:0;width:100vw;height:100vh;background:#f3f7fb;color:#122238;
font-family:'Noto Sans',Arial,sans-serif;display:flex;align-items:center;justify-content:center}
.wrap{width:1480px}.k{font-size:17px;letter-spacing:.14em;text-transform:uppercase;color:#0070e0;font-weight:700}
h1{font-size:40px;margin:8px 0 34px;font-weight:750}h1 span{color:#0070e0}
.row{display:grid;grid-template-columns:300px 60px 560px 60px 440px;align-items:center}
.node{background:#fff;border:1px solid #cfdcea;border-radius:16px;padding:20px 22px;box-shadow:0 6px 18px rgba(18,34,56,.06)}
.node h3{margin:0;font-size:24px;display:flex;align-items:center;gap:12px}.node p{margin:8px 0 0;color:#607089;font-size:17px;line-height:1.4}
.ic{width:30px;height:30px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none}
.ib{width:46px;height:46px;border-radius:12px;display:flex;align-items:center;justify-content:center;flex:none}
.agent .ib{background:#eef2f7;color:#2d405a}.pp .ib{background:#eaf3ff;color:#003087}
.guard{border:2px solid #0070e0;background:#fff;border-radius:18px;padding:18px 20px;box-shadow:0 10px 26px rgba(0,112,224,.12)}
.guard h3{margin:0 0 12px;font-size:23px;display:flex;align-items:center;gap:12px;color:#003087}
.g{display:flex;align-items:center;gap:14px;padding:9px 12px;border-radius:10px;background:#f2f8ff;margin-top:8px;font-size:19px}
.g b{min-width:150px}.g span{color:#607089;font-size:16px}.g .ic{width:24px;height:24px;color:#0070e0}
.arrow{display:flex;justify-content:center;color:#8a99ad}.arrow .ic{width:40px;height:40px}
.pps{display:flex;flex-direction:column;gap:18px}
.audit{margin-top:34px;background:#122238;color:#fff;border-radius:16px;padding:18px 26px;display:flex;align-items:center;gap:16px;font-size:21px}
.audit .ic{color:#a8e6b8}.audit span{color:#c9d8ee;font-size:17px;margin-left:auto}
.f{opacity:0;transform:translateY(12px);transition:all .55s ease}.go .f{opacity:1;transform:none}
.go .d1{transition-delay:.0s}.go .d2{transition-delay:.5s}.go .d3{transition-delay:.9s}.go .d4{transition-delay:1.3s}
.go .d5{transition-delay:1.6s}.go .d6{transition-delay:1.9s}.go .d7{transition-delay:2.2s}.go .d8{transition-delay:2.6s}
.go .d9{transition-delay:3.0s}.go .d10{transition-delay:3.5s}"""


def arch_html():
    with open(os.path.join(ROOT, "web", "static", "icons", "lucide-sprite.svg"), encoding="utf-8") as f:
        sprite = f.read()
    sprite = sprite.replace("<svg ", "<svg style='display:none' ", 1).replace("</svg>", BOT_ICON + "</svg>")
    ic = lambda n, cls="ic": f"<svg class='{cls}'><use href='#lucide-{n}'/></svg>"
    rows = [("scale", "Policy", "budgets, categories, hard cap"), ("signature", "AP2 mandate", "Ed25519-signed limits"),
            ("shield-alert", "Scope guard", "supplier text is untrusted"), ("user-check", "Approvals", "humans only, no approve tool"),
            ("package-check", "Receiving", "delivery checked against order")]
    g = "".join(f"<div class='g f d{3 + i}'>{ic(n)}<b>{t}</b><span>{d}</span></div>" for i, (n, t, d) in enumerate(rows))
    return f"""<!doctype html><meta charset=utf-8><style>{ARCH_CSS}</style>{sprite}<div class=wrap id=w>
<div class='k f d1'>How it works</div><h1 class='f d1'>The LLM proposes. <span>Code decides.</span> PayPal moves the money.</h1>
<div class=row>
 <div class='node agent f d1'><h3><div class=ib>{ic('bot')}</div>LLM agent</h3><p>Reads the request, compares quotes, calls tools. Holds no payment authority.</p></div>
 <div class='arrow f d2'>{ic('arrow-right')}</div>
 <div class='guard f d2'><h3>{ic('shield-check')}GuardedBuyer guardrail layer</h3>{g}</div>
 <div class='arrow f d8'>{ic('arrow-right')}</div>
 <div class=pps><div class='node pp f d8'><h3><div class=ib>{ic('credit-card')}</div>PayPal Orders v2</h3><p>create, buyer approval, capture</p></div>
  <div class='node pp f d9'><h3><div class=ib>{ic('receipt')}</div>PayPal Disputes v1</h3><p>drafted claim, evidence, tracking</p></div></div>
</div>
<div class='audit f d10'>{ic('link')}<b>Hash-chained audit log</b><span>every decision, block and payment · SHA-256 chain · tamper-evident</span></div>
</div>"""


def card_pages(work, smoke_lines, cmdline):
    title = f"""<!doctype html><meta charset=utf-8><style>{CARD_CSS}</style><div class=wrap>
<div class=brand><div class=mark>{SHIELD}</div><div style='font-size:30px;font-weight:700'>GuardedBuyer</div></div>
<p class=l id=a>AI agents can now shop and pay for a business.</p>
<p class=l id=b><span class=r>One poisoned listing</span> can redirect the money.</p>
<p class=l id=c><span class=r>Short deliveries</span> rarely get claimed back.</p></div>"""
    arch = arch_html()
    close = f"""<!doctype html><meta charset=utf-8><style>{CARD_CSS}</style><div class=wrap>
<div class=brand><div class=mark>{SHIELD}</div><div style='font-size:30px;font-weight:700'>GuardedBuyer</div></div>
<p class='l on'>Let AI do the buying,<br>without handing it the keys.</p>
<div class=chips><span class=chip>PayPal Orders v2 + Disputes v1</span><span class=chip>AP2 Intent Mandates</span>
<span class=chip>LLM tool-calling agent</span><span class=chip>Human approval desk</span><span class=chip>Hash-chained audit</span>
<span class=chip>AG Grid evaluation</span></div><p class=s>Open source (MIT) · © 2026 elidot. All demo data is synthetic.</p></div>"""
    rows = "".join(
        f"<div class='ln {('ok' if l.startswith('[ok]') else 'exp' if l.startswith('[expected]') else 'hd')}'>{escape(l)}</div>"
        for l in smoke_lines)
    term = f"""<!doctype html><meta charset=utf-8><style>body{{margin:0;background:#0d1117;color:#d6e2f0;
font:19px/1.55 'DejaVu Sans Mono',monospace;padding:46px 56px}}.bar{{color:#7d8da1;margin-bottom:18px}}
.ln{{white-space:pre-wrap;word-break:break-all;opacity:0}}.ln.show{{opacity:1}}.ok{{color:#a8e6b8}}.exp{{color:#ffd28a}}
.hd{{color:#9cc7ff}}</style><div class=bar>$ {escape(cmdline)}</div>{rows}"""
    paths = {}
    for k, html in (("title", title), ("architecture", arch), ("close", close), ("terminal", term)):
        p = os.path.join(work, f"card_{k}.html")
        with open(p, "w", encoding="utf-8") as f: f.write(html)
        paths[k] = "file://" + p
    return paths


# ---------------------------------------------------------------- CDP plumbing
class CDP:
    def __init__(self, ws):
        self.ws, self.seq, self.pending, self.errors = ws, 0, {}, []

    async def reader(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
            elif msg.get("method") == "Runtime.exceptionThrown":
                self.errors.append(msg["params"]["exceptionDetails"].get("text"))

    async def send(self, method, params=None, timeout=30):
        self.seq += 1; i = self.seq
        fut = asyncio.get_event_loop().create_future(); self.pending[i] = fut
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        try:
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(i, None); raise
        if "error" in msg: raise RuntimeError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def js(self, expr, await_promise=True):
        r = await self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": await_promise, "returnByValue": True})
        if r.get("exceptionDetails"): raise RuntimeError(f"JS error in {expr[:80]}: {r['exceptionDetails']}")
        return r.get("result", {}).get("value")

    async def goto(self, url, ready_expr="document.readyState === 'complete'", timeout=25):
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(0.3)
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if await self.js(f"!!({ready_expr})", False): break
            except Exception:
                pass
            await asyncio.sleep(0.15)
        await self.js(CURSOR_JS, False)


CURSOR_JS = r"""
(function(){ if (window.__gb) return; window.__gb = 1;
 var c = document.createElement('div'); c.id='__cur';
 c.style.cssText='position:fixed;left:800px;top:520px;width:22px;height:22px;z-index:2147483647;pointer-events:none;transition:left .55s cubic-bezier(.3,.7,.2,1),top .55s cubic-bezier(.3,.7,.2,1)';
 c.innerHTML="<svg width='22' height='22' viewBox='0 0 24 24'><path d='M4 3l7 17 2.5-7.5L21 10z' fill='#111' stroke='#fff' stroke-width='1.6' stroke-linejoin='round'/></svg>";
 document.documentElement.appendChild(c);
 window.__move = function(x,y){ c.style.left=x+'px'; c.style.top=y+'px'; };
 window.__ripple = function(x,y){ var r=document.createElement('div');
   r.style.cssText='position:fixed;left:'+(x-18)+'px;top:'+(y-18)+'px;width:36px;height:36px;border-radius:50%;border:3px solid #0070e0;z-index:2147483646;pointer-events:none;transition:all .45s ease-out;opacity:.9';
   document.documentElement.appendChild(r); requestAnimationFrame(function(){ r.style.transform='scale(1.8)'; r.style.opacity='0'; });
   setTimeout(function(){ r.remove(); }, 600); };
 window.__find = function(sel, text){ var list = Array.prototype.slice.call(document.querySelectorAll(sel));
   if (text) list = list.filter(function(e){ return (e.textContent||'').indexOf(text) >= 0; });
   return list[0] || null; };
 // digital zoom: centre an element and scale the page so small UI text stays readable at 1080p; amber focus ring
 window.__zoomEl = null; window.__ring = null;
 // union of the text boxes inside e (wide containers with short text would otherwise barely zoom)
 window.__textRect = function(e){ var w = document.createTreeWalker(e, NodeFilter.SHOW_TEXT), n, L=1e9, T=1e9, R=-1e9, B=-1e9;
   while ((n = w.nextNode())) { if (!n.textContent.trim()) continue; var r = document.createRange(); r.selectNodeContents(n);
     var rs = r.getClientRects(); for (var i = 0; i < rs.length; i++) { var q = rs[i]; if (!q.width || !q.height) continue;
       L = Math.min(L, q.left); T = Math.min(T, q.top); R = Math.max(R, q.right); B = Math.max(B, q.bottom); } }
   if (R < L) return e.getBoundingClientRect(); return {left: L, top: T, width: R - L, height: B - T}; };
 window.__zoom = function(sel, text, maxScale, child, force, narrow){ var e = window.__find(sel, text); if (e && child) e = e.querySelector(child);
   if (!e) return false; var h = document.documentElement;
   h.style.transition = 'none'; h.style.transform = '';
   if (narrow) { e.dataset.zoomMw = e.style.maxWidth || ''; e.style.maxWidth = narrow + 'px'; }
   e.scrollIntoView({block: 'center', behavior: 'instant'});
   var b = window.__textRect(e), W = innerWidth, H = innerHeight, pad = 10;
   var s = force ? maxScale : Math.max(1.25, Math.min(maxScale || 2.2, 0.9 * W / (b.width + 2 * pad), 0.78 * H / (b.height + 2 * pad)));
   var cx = b.left + b.width / 2, cy = b.top + b.height / 2;
   h.style.transformOrigin = (cx + scrollX) + 'px ' + (cy + scrollY) + 'px';
   var ring = document.createElement('div');
   ring.style.cssText = 'position:absolute;pointer-events:none;z-index:2147483640;border-radius:10px;box-shadow:0 0 0 3px #f5a524, 0 0 0 9px rgba(245,165,36,.25);'
     + 'left:' + (b.left + scrollX - pad) + 'px;top:' + (b.top + scrollY - pad) + 'px;width:' + (b.width + 2 * pad) + 'px;height:' + (b.height + 2 * pad) + 'px';
   document.body.appendChild(ring); window.__ring = ring; window.__zoomEl = e;
   c.style.opacity = '0';
   requestAnimationFrame(function(){ h.style.transition = 'transform .7s cubic-bezier(.3,.7,.2,1)';
     h.style.transform = 'translate(' + (W / 2 - cx) + 'px,' + (H / 2 - cy) + 'px) scale(' + s + ')'; });
   return s; };
 window.__unzoom = function(){ var h = document.documentElement; h.style.transition = 'transform .6s cubic-bezier(.3,.7,.2,1)';
   h.style.transform = ''; if (window.__ring) window.__ring.remove(); var e = window.__zoomEl;
   if (e && e.dataset.zoomMw !== undefined) { var mw = e.dataset.zoomMw; setTimeout(function(){ e.style.maxWidth = mw; }, 600); delete e.dataset.zoomMw; }
   window.__zoomEl = null; window.__ring = null; setTimeout(function(){ c.style.opacity = '1'; }, 600); };
 window.__badge = function(txt, ms){ var d = document.createElement('div');
   d.style.cssText = 'position:fixed;right:24px;top:72px;z-index:2147483645;background:#122238;color:#fff;font:600 15px/1.3 Noto Sans,Arial,sans-serif;padding:9px 14px;border-radius:10px;opacity:.94';
   d.textContent = txt; document.documentElement.appendChild(d); setTimeout(function(){ d.remove(); }, ms || 2500); };
 window.__hl = function(sel, text, on){ var e = window.__find(sel, text); if (!e) return false;
   e.style.transition='box-shadow .4s'; e.style.boxShadow = on ? '0 0 0 4px rgba(0,112,224,.55)' : ''; return true; };
})();
"""


class Recorder:
    def __init__(self, cdp, frames_dir):
        self.cdp, self.dir, self.frames, self.running = cdp, frames_dir, [], False

    async def loop(self):
        n = 0
        while self.running:
            t = time.monotonic()
            try:
                r = await self.cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 88, "fromSurface": True},
                                        timeout=1.5)
                p = os.path.join(self.dir, f"f{n:06d}.jpg")
                with open(p, "wb") as f: f.write(base64.b64decode(r["data"]))
                self.frames.append((time.monotonic(), p)); n += 1
            except Exception:
                pass
            await asyncio.sleep(max(0, 1 / FPS_CAPTURE - (time.monotonic() - t)))


# ---------------------------------------------------------------- scene actions
async def at(t0, t):
    d = t0 + t - time.monotonic()
    if d > 0: await asyncio.sleep(d)


async def move_to(cdp, sel, text=None, scroll=True):
    expr = f"""(async function(){{ var e = __find({json.dumps(sel)}, {json.dumps(text)}); if (!e) return null;
      {"e.scrollIntoView({behavior:'smooth', block:'center'}); await new Promise(r=>setTimeout(r,650));" if scroll else ""}
      var b = e.getBoundingClientRect(); var x = b.left + Math.min(b.width/2, 160), y = b.top + b.height/2;
      __move(x, y); return [x, y]; }})()"""
    pos, t = None, time.time()
    while pos is None and time.time() - t < 10:
        pos = await cdp.js(expr)
        if pos is None: await asyncio.sleep(0.3)
    if pos is None: raise RuntimeError(f"element not found: {sel} {text}")
    await asyncio.sleep(0.6)
    return pos


async def click(cdp, sel, text=None):
    x, y = await move_to(cdp, sel, text)
    await cdp.js(f"__ripple({x},{y})", False)
    await asyncio.sleep(0.15)
    await cdp.js(f"(function(){{ var e = __find({json.dumps(sel)}, {json.dumps(text)}); e.click(); }})()", False)


async def click_nav(cdp, sel, text, path_prefix, timeout=15):
    """Click something that navigates, then wait for the new page (path starts with path_prefix) and re-inject the cursor."""
    x, y = await move_to(cdp, sel, text)
    await cdp.js(f"__ripple({x},{y})", False)
    await asyncio.sleep(0.2)
    await cdp.js(f"(function(){{ var e = __find({json.dumps(sel)}, {json.dumps(text)}); setTimeout(function(){{ e.click(); }}, 30); }})()", False)
    t = time.time()
    while time.time() - t < timeout:
        await asyncio.sleep(0.2)
        try:
            ok = await cdp.send("Runtime.evaluate", {"expression": f"location.pathname.indexOf({json.dumps(path_prefix)}) === 0 && document.readyState === 'complete'",
                                                     "returnByValue": True}, timeout=2)
            if ok.get("result", {}).get("value"): break
        except Exception:
            pass
    await asyncio.sleep(0.3)
    await cdp.js(CURSOR_JS, False)
    await cdp.js(f"__move({x},{y})", False)


async def zoom(cdp, sel, text=None, hold=3.0, scale=2.2, child=None, force=False, narrow=None):
    """Digital zoom on one element for `hold` seconds (CSS transform on <html>; text re-rasterises sharply)."""
    for _ in range(30):
        s = await cdp.js(f"__zoom({json.dumps(sel)}, {json.dumps(text)}, {scale}, {json.dumps(child)}, {json.dumps(force)}, {json.dumps(narrow)})", False)
        if s: break
        await asyncio.sleep(0.3)
    else:
        raise RuntimeError(f"zoom target not found: {sel} {text} {child}")
    await asyncio.sleep(hold)
    await cdp.js("__unzoom()", False)
    await asyncio.sleep(0.7)


async def scroll_to(cdp, y_expr, smooth=True):
    await cdp.js(f"window.scrollTo({{top: {y_expr}, behavior: '{'smooth' if smooth else 'instant'}'}})", False)


async def scroll_el(cdp, sel, text=None, block="start", offset=90):
    await cdp.js(f"""(function(){{ var e = __find({json.dumps(sel)}, {json.dumps(text)}); if (!e) return;
      var y = e.getBoundingClientRect().top + window.scrollY - {offset}; window.scrollTo({{top: y, behavior: 'smooth'}}); }})()""", False)


async def type_into(cdp, sel, text):
    await move_to(cdp, sel)
    await cdp.js(f"(function(){{ var e=document.querySelector({json.dumps(sel)}); e.focus(); }})()", False)
    for ch in text:
        await cdp.js(f"(function(){{ var e=document.querySelector({json.dumps(sel)}); e.value += {json.dumps(ch)}; }})()", False)
        await asyncio.sleep(0.035)


async def scene_title(cdp, base, cards, t0, sc):
    for k, idx in (("a", 0), ("b", 1), ("c", 1)):
        st = sc["items"][idx]["start"] + (sc["items"][1]["dur"] * 0.55 if k == "c" else 0)
        await at(t0, st)
        await cdp.js(f"document.getElementById('{k}').classList.add('on')", False)


async def scene_architecture(cdp, base, cards, t0, sc):
    await cdp.js("document.getElementById('w').classList.add('go')", False)


async def scene_dashboard(cdp, base, cards, t0, sc):
    await move_to(cdp, ".brand-name", scroll=False)
    await at(t0, sc["items"][1]["start"])
    await scroll_el(cdp, "#quotes", offset=110)
    await asyncio.sleep(1.2)
    await move_to(cdp, "#quotes", scroll=False)
    await asyncio.sleep(2.5)
    await scroll_el(cdp, "#policy", offset=110)


async def scene_desk_blocked(cdp, base, cards, t0, sc):
    await asyncio.sleep(0.3)
    await move_to(cdp, ".banner", scroll=False)
    await asyncio.sleep(0.6)
    await zoom(cdp, ".panel", "Why it was blocked", hold=3.0, scale=2.0, child="ul.reasons")          # policy block reasons
    await at(t0, sc["items"][1]["start"])
    await zoom(cdp, ".panel", "Why it was blocked", hold=3.2, scale=2.0, child="ul.reasons:last-of-type")  # refused attempt
    await at(t0, sc["items"][2]["start"])
    await scroll_el(cdp, ".panel", "Request and chosen quote")
    await asyncio.sleep(1.2)
    await zoom(cdp, ".panel", "AP2 mandate", hold=3.4, scale=2.0, child="dl.kv")                       # constraints to be signed


async def scene_desk_release(cdp, base, cards, t0, sc):
    await scroll_el(cdp, ".panel", "Approval chain")
    await asyncio.sleep(0.6)
    await type_into(cdp, "#note", "Budget moved from Q1 onboarding")
    await click(cdp, "button", "Approve as manager")
    await asyncio.sleep(1.2)
    await click(cdp, "button", "Approve as finance")
    await at(t0, sc["items"][1]["start"])
    await scroll_el(cdp, ".panel", "PayPal order")
    await asyncio.sleep(0.5)
    await click(cdp, "button", "Release & place PayPal order")
    t_click = time.monotonic()
    while time.monotonic() - t_click < 60:     # mandate is signed, verified, then the real sandbox order is created
        if await cdp.js("!!__find('.panel', 'Signature valid') && !!__find('.panel dd', 'PAYER_ACTION_REQUIRED')", False): break
        await asyncio.sleep(0.25)
    waited = time.monotonic() - t_click
    if waited > 3.0:
        SKIPS["desk_release"] = (t_click + 1.2, time.monotonic() - 0.2)
        await cdp.js(f"__badge('Real PayPal sandbox round trip: {waited:.0f} s of waiting cut', 2600)", False)
        print(f"[release] order visible after {waited:.1f}s; cut {SKIPS['desk_release'][1] - SKIPS['desk_release'][0]:.1f}s")
    await zoom(cdp, ".panel", "Signature valid", hold=3.4, scale=2.0, child="dl.kv")                   # signed mandate


VIDEO_IDS = {}
SKIPS = {}       # scene -> (monotonic start, end) of a stretch removed from the recording
PP_IDS_JS = """(function(){ var p = __find('.panel', 'PayPal order'); if (!p) return null; var dd = p.querySelectorAll('dl.kv dd');
  return {order_id: dd[0] ? dd[0].textContent.trim() : null, order_status: dd[1] ? dd[1].textContent.trim() : null,
          capture_id: dd[2] ? dd[2].textContent.trim() : null, amount: dd[3] ? dd[3].textContent.trim() : null}; })()"""


async def scene_paypal(cdp, base, cards, t0, sc):
    await scroll_el(cdp, ".panel", "PayPal order")
    await asyncio.sleep(0.4)
    await zoom(cdp, ".panel", "PayPal order", hold=3.0, scale=2.4, child="dl.kv")                    # sandbox order ID
    VIDEO_IDS["at_order"] = await cdp.js(PP_IDS_JS, False)
    await at(t0, sc["items"][1]["start"] + 0.4)
    await click(cdp, "button", "Approve with sandbox test card")
    t_click = time.monotonic(); ids = None
    while time.monotonic() - t_click < 60:
        ids = await cdp.js(PP_IDS_JS, False)
        if ids and ids.get("capture_id") and "not captured" not in ids["capture_id"]: break
        await asyncio.sleep(0.25)
    t_cap = time.monotonic(); VIDEO_IDS["at_capture"] = ids
    waited = t_cap - t_click
    if waited > 3.0:     # the real sandbox round trips can take 10+ s: skip the dead wait, say so on screen
        SKIPS["paypal"] = (t_click + 1.5, t_cap - 0.2)
        cut = SKIPS["paypal"][1] - SKIPS["paypal"][0]; t0 += cut
        await cdp.js(f"__badge('Real PayPal sandbox round trip: {waited:.0f} s of waiting cut', 2600)", False)
        print(f"[paypal] capture visible after {waited:.1f}s; cut {cut:.1f}s of waiting from the recording")
    await at(t0, sc["items"][2]["start"])
    await zoom(cdp, ".panel", "PayPal order", hold=3.2, scale=2.4, child="dl.kv")                    # capture ID
    await zoom(cdp, ".panel", "Audit trail", hold=2.6, scale=1.9, child="tbody tr:last-child td:nth-child(4)", force=True)


async def scene_mismatch(cdp, base, cards, t0, sc):
    item = await cdp.js("(document.querySelector('.detail-hero')||{}).textContent || ''", False)
    VIDEO_IDS["mismatch_item"] = " ".join(item.split())[:160]
    if "toner" not in item.lower(): print(f"[warn] mismatch scene does not show the toner ticket: {item[:80]!r}")
    await asyncio.sleep(0.3)
    await zoom(cdp, ".detail-hero", None, hold=2.8, scale=2.2, child=".hero-main")                                     # 4 x toner, $220
    await at(t0, sc["items"][1]["start"])
    await zoom(cdp, ".dispute-message", None, hold=3.8, scale=2.2, narrow=620)                                   # got 3 of 4, refund $55
    await at(t0, sc["items"][2]["start"])
    await scroll_el(cdp, "#dispute", offset=100)


async def scene_tamper(cdp, base, cards, t0, sc):
    await scroll_el(cdp, "#audit", offset=90)
    await asyncio.sleep(0.5)
    await click(cdp, "#tamper-btn")
    await move_to(cdp, "#audit-result:not([hidden])", scroll=False)
    await zoom(cdp, "#audit-result", None, hold=2.6, scale=2.2)                                      # chain breaks at #N


async def scene_red_team(cdp, base, cards, t0, sc):
    await asyncio.sleep(0.3)
    await zoom(cdp, ".untrusted", None, hold=3.8, scale=2.2, narrow=600)                                         # injection payload
    await at(t0, sc["items"][1]["start"])
    await zoom(cdp, ".trace-step.blocked", None, hold=3.0, scale=2.0, narrow=760)                                # blocked self-approval
    await scroll_el(cdp, ".trace-step.blocked", offset=120)
    await asyncio.sleep(0.6)
    await cdp.js("window.scrollBy({top: 420, behavior: 'smooth'})", False)


async def scene_evaluation(cdp, base, cards, t0, sc):
    await move_to(cdp, ".eval-card", "No guardrails", scroll=False)
    await at(t0, sc["items"][1]["start"])
    await move_to(cdp, ".eval-card.is-ours", scroll=False)
    await asyncio.sleep(3.0)
    await move_to(cdp, ".eval-card", "Policy only", scroll=False)
    await at(t0, sc["items"][2]["start"])
    await click(cdp, "#type-filter button", "Missed by full")
    await asyncio.sleep(0.8)
    await scroll_el(cdp, ".eval-grid-panel", offset=90)


async def scene_terminal(cdp, base, cards, t0, sc):
    n = await cdp.js("document.querySelectorAll('.ln').length", False)
    span = max(1.0, sc["items"][0]["dur"] - 0.5)
    for i in range(n):
        await cdp.js(f"document.querySelectorAll('.ln')[{i}].classList.add('show')", False)
        await asyncio.sleep(span / n)


async def scene_close(cdp, base, cards, t0, sc):
    await asyncio.sleep(0.1)


READY = "document.body.dataset.ready === '1'"
# scene -> (url or card key, replay scenario to run first, readiness expression); done before the scene clock starts
PREP = {"title": ("card:title", None, None), "architecture": ("card:architecture", None, None), "dashboard": ("/", "normal", READY),
        "desk_blocked": ("/approvals?demo=1", None, "document.querySelector('.banner') && document.querySelector('.budget-bar')"),
        "mismatch": ("/?panel=receiving", "mismatch", READY), "red_team": ("/?panel=trace&trace=red_team", "red_team", READY),
        "evaluation": ("/evaluation", None, READY), "terminal": ("card:terminal", None, None), "close": ("card:close", None, None)}


def api_replay(base, scenario):
    req = urllib.request.Request(base + "/api/replay", data=json.dumps({"scenario": scenario, "reset": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        r.read()


SANDBOX_BASE = {"url": None}
DESK_SCENES = ("desk_blocked", "desk_release", "paypal")


async def prepare(cdp, base, cards, name):
    if name not in PREP: return
    if name in DESK_SCENES and SANDBOX_BASE["url"]: base = SANDBOX_BASE["url"]
    url, scenario, ready = PREP[name]
    if scenario:
        t = time.time(); await asyncio.to_thread(api_replay, base, scenario)
        print(f"[prep] replay {scenario} took {time.time() - t:.1f}s")
    if url.startswith("card:"):
        await cdp.goto(cards[url[5:]])
    else:
        await cdp.goto(base + url, ready or "document.readyState === 'complete'", timeout=120)
    await asyncio.sleep(0.6)


ACTIONS = {"title": scene_title, "architecture": scene_architecture, "tamper": scene_tamper, "dashboard": scene_dashboard, "desk_blocked": scene_desk_blocked,
           "desk_release": scene_desk_release, "paypal": scene_paypal, "mismatch": scene_mismatch,
           "red_team": scene_red_team, "evaluation": scene_evaluation, "terminal": scene_terminal, "close": scene_close}


async def record(plan, base, cards, work):
    port = 9351
    profile = tempfile.mkdtemp(prefix="gb-video-")
    chrome = subprocess.Popen(["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                               "--hide-scrollbars", "--allow-file-access-from-files", f"--remote-debugging-port={port}",
                               f"--user-data-dir={profile}", f"--window-size={W},{H}", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(150):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as r:
                    page = next((t for t in json.load(r) if t.get("type") == "page"), None)
                if page: ws_url = page["webSocketDebuggerUrl"]; break
            except Exception:
                pass
            await asyncio.sleep(0.15)
        async with websockets.connect(ws_url, max_size=50_000_000) as ws:
            cdp = CDP(ws); reader = asyncio.create_task(cdp.reader())
            await cdp.send("Page.enable"); await cdp.send("Runtime.enable")
            await cdp.send("Emulation.setDeviceMetricsOverride", {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False})
            segments = []
            for si, sc in enumerate(plan):
                fdir = os.path.join(work, f"frames_{si:02d}"); shutil.rmtree(fdir, ignore_errors=True); os.makedirs(fdir)
                # prepare the first view before the clock starts so scenes don't open on a loading screen
                await prepare(cdp, base, cards, sc["name"])
                rec = Recorder(cdp, fdir)
                t0 = time.monotonic()
                act = asyncio.create_task(ACTIONS[sc["name"]](cdp, base, cards, t0 + 0.0, sc))
                await asyncio.sleep(0.25)
                rec.running = True; loop = asyncio.create_task(rec.loop())
                t_rec0 = time.monotonic()
                try:
                    await asyncio.wait_for(act, timeout=sc["length"] + 75)
                except Exception as e:
                    print(f"[warn] scene {sc['name']}: {type(e).__name__}: {e}")
                skip = SKIPS.get(sc["name"])
                await at(t_rec0, sc["length"] + ((skip[1] - skip[0]) if skip else 0))
                rec.running = False; await loop
                frames = rec.frames
                if skip:
                    a_, b_ = skip; cut = b_ - a_
                    frames = [(t if t < a_ else t - cut, p) for t, p in frames if not (a_ <= t < b_)]
                segments.append((t_rec0, frames, sc["length"]))
                print(f"[rec] {sc['name']:<13} {sc['length']:6.2f}s  frames={len(rec.frames)}")
            reader.cancel()
            return segments, cdp.errors
    finally:
        chrome.terminate()
        try: chrome.wait(timeout=5)
        except Exception: chrome.kill()
        shutil.rmtree(profile, ignore_errors=True)


def build_video(segments, audio, srt, out_mp4, work):
    concat = os.path.join(work, "frames.txt")
    with open(concat, "w") as f:
        for t_rec0, frames, length in segments:
            if not frames: continue
            for i, (t, p) in enumerate(frames):
                start = 0.0 if i == 0 else t - t_rec0
                end = (frames[i + 1][0] - t_rec0) if i + 1 < len(frames) else length
                d = max(0.01, min(end, length) - start)
                f.write(f"file '{p}'\nduration {d:.4f}\n")
        f.write(f"file '{segments[-1][1][-1][1]}'\n")
    style = ("FontName=Noto Sans,FontSize=12,PrimaryColour=&H00FFFFFF,BackColour=&H96000000,BorderStyle=4,Outline=0,"
             "Shadow=0,MarginV=16,Alignment=2")
    srt_esc = srt.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", concat, "-i", audio,
         "-vf", f"scale=1920:1080:flags=lanczos,fps=30,subtitles='{srt_esc}':force_style='{style}',format=yuv420p",
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
         "-shortest", "-movflags", "+faststart", out_mp4])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "materials"))
    ap.add_argument("--work", default=os.path.join(tempfile.gettempdir(), "gb_video_work"))
    ap.add_argument("--voice", default="en-US-AndrewNeural")
    ap.add_argument("--rate", default="+4%")
    ap.add_argument("--port", type=int, default=8031)
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--no-music", action="store_true", help="skip the self-synthesized ambient pad")
    ap.add_argument("--llm", action="store_true", help="run the demo server with the real LLM (LLM_ENV_FILE / .env); the key is never shown")
    a = ap.parse_args(argv)
    if not a.sandbox:
        raise SystemExit("this cut records the PayPal scenes on the real sandbox: pass --sandbox (PAYPAL_SECRETS_FILE or .secrets)")
    os.makedirs(a.out, exist_ok=True); os.makedirs(a.work, exist_ok=True)
    plan = asyncio.run(tts_all(a.work, a.voice, a.rate))
    total = sum(s["length"] for s in plan)
    print(f"[tts] {sum(len(s['items']) for s in plan)} sentences, planned length {total:.1f}s")
    if total >= 175: raise SystemExit(f"planned length {total:.1f}s is too long (limit 175s)")
    voice_wav, _ = build_audio(plan, a.work)
    audio = mix_music(voice_wav, os.path.join(a.work, "mix.wav")) if not a.no_music else voice_wav
    srt = os.path.join(a.out, "demo_video.srt"); build_srt(plan, srt)
    smoke_args = ["--test-card"] if a.sandbox else ["--fake"]
    smoke = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "sandbox_smoke.py")] + smoke_args, cwd=ROOT,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout.splitlines()
    smoke = [l for l in smoke if l.startswith(("[ok]", "[expected]", "ALL STEPS", "=="))]
    smoke = [re.sub(r"\s*client_id=.*?chars\)", "", l.split("  -> out/")[0]) for l in smoke]
    smoke = [re.sub(r"approve_url=\S+", "", l) for l in smoke]
    if any(("secret" in l.lower()) for l in smoke): raise SystemExit("refusing: smoke output mentions a secret")
    cards = card_pages(a.work, smoke, "python3 scripts/sandbox_smoke.py " + " ".join(smoke_args)
                       + ("      # real PayPal sandbox, credentials from a git-ignored file" if a.sandbox else ""))
    servers = []
    def start(port, extra):
        cmd = [sys.executable, os.path.join(ROOT, "web", "server.py"), "--port", str(port)] + extra + (["--llm"] if a.llm else [])
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); servers.append(p)
        print(f"[srv] pid={p.pid} port={port} {' '.join(extra) or '(offline emulation)'}")
        b = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try: urllib.request.urlopen(b + "/api/state", timeout=1); break
            except Exception: time.sleep(0.1)
        return b
    try:
        base = start(a.port, [])
        SANDBOX_BASE["url"] = start(a.port + 1, ["--sandbox"]) if a.sandbox else base
        segments, errors = asyncio.run(record(plan, base, cards, a.work))
    finally:
        for p in servers:
            p.send_signal(signal.SIGTERM)
            try: p.wait(timeout=5)
            except Exception: p.kill()
            print(f"[srv] stopped pid={p.pid}")
    out_mp4 = os.path.join(a.out, "demo_video.mp4")
    build_video(segments, audio, srt, out_mp4, a.work)
    with open(os.path.join(a.out, "demo_video_narration.txt"), "w", encoding="utf-8") as f:
        for s in plan:
            f.write(f"[{s['name']}]\n" + "\n".join(it["text"] for it in s["items"]) + "\n\n")
    with open(os.path.join(a.out, "demo_video_ids.json"), "w", encoding="utf-8") as f:
        json.dump({"note": "IDs read from the recorded page (real PayPal sandbox when --sandbox)", **VIDEO_IDS}, f, indent=1)
    print("[ids]", json.dumps(VIDEO_IDS))
    print(f"[out] {out_mp4}  duration={duration(out_mp4):.2f}s  js_errors={len(errors)}")
    for e in errors: print("  js:", e)


if __name__ == "__main__":
    main()
