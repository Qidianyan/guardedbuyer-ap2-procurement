"""Video builder helpers: self-synthesized music, mixing level, architecture card, narration consistency."""
import os, re, sys, tempfile, unittest, wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts", "video")); sys.path.insert(0, ROOT)
try:
    import numpy as np
    import make_demo_video as V
except ImportError as e:                       # websockets / numpy are only needed for the video builder
    V = None; _why = str(e)


@unittest.skipIf(V is None, "video builder deps missing")
class VideoBuilderTest(unittest.TestCase):
    def test_pad_is_deterministic_and_bounded(self):
        a, b = V.synth_pad(6.0), V.synth_pad(6.0)
        self.assertEqual(len(a), 6 * 48000)
        self.assertTrue(np.array_equal(a, b))
        self.assertFalse(np.isnan(a).any()); self.assertAlmostEqual(float(np.abs(a).max()), 1.0, places=3)
        self.assertLess(abs(float(a[0])), 1e-3)          # fades in from silence

    def test_mix_keeps_music_far_below_voice(self):
        sr = 48000; t = np.arange(sr * 10) / sr
        voice = np.where((t > 5) & (t < 7), 0.3 * np.sin(2 * np.pi * 220 * t), 0).astype(np.float32)
        d = tempfile.mkdtemp(); src, out = os.path.join(d, "v.wav"), os.path.join(d, "m.wav")
        with wave.open(src, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes((voice * 32767).astype(np.int16).tobytes())
        V.mix_music(src, out)
        with wave.open(out) as w:
            mix = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        music_only = mix[int(3.0 * sr):int(4.0 * sr)]                     # after the fade-in, before speech
        under_voice = (mix - voice)[int(5.8 * sr):int(6.2 * sr)]          # music while speaking
        rms = lambda x: 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)
        self.assertLess(rms(music_only), -35); self.assertGreater(rms(music_only), -60)
        self.assertLess(rms(under_voice), rms(music_only) - 4)           # ducked under the narration
        self.assertLess(rms(under_voice), rms(voice[int(5.8 * sr):int(6.2 * sr)]) - 25)

    def test_architecture_card(self):
        html = V.arch_html()
        for word in ("LLM agent", "Policy", "AP2 mandate", "Scope guard", "Approvals", "Receiving",
                     "PayPal Orders v2", "PayPal Disputes v1", "Hash-chained audit"):
            self.assertIn(word, html)
        self.assertNotRegex(html, r"(src|href)\s*=\s*['\"]?https?://")     # no external assets (sprite is inlined)
        self.assertIn("lucide", html.lower())

    def test_narration_matches_mismatch_scenario(self):
        text = " ".join(" ".join(s) for n, s in V.SCENES if n == "mismatch")
        with open(os.path.join(ROOT, "scenarios", "run_all.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"Order 4 black toner cartridges"', src)
        self.assertIn('"quantity": req.quantity - 1', src)
        self.assertIn("four black toner cartridges", text); self.assertIn("only three arrive", text)
        self.assertNotIn("paper", text.lower())

    def test_scene_order_and_length_budget(self):
        names = [n for n, _ in V.SCENES]
        self.assertEqual(names[:2], ["title", "architecture"]); self.assertIn("tamper", names)
        words = sum(len(x.split()) for _, s in V.SCENES for x in s)
        self.assertLess(words, 175 / 60 * 165)            # < 2:55 at ~165 wpm before pauses

    def test_captions_split(self):
        for s in (x for _, ss in V.SCENES for x in ss):
            for c in V.split_caption(s): self.assertLessEqual(len(c), 78)


if __name__ == "__main__":
    unittest.main()
