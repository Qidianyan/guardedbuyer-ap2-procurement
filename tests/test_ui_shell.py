"""Product shell checks: every page shares the nav, share metadata, favicon, one icon library, and
friendly empty / not-found states. Offline loopback HTTP, no browser."""
import os, re, sys, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web.server as WS

EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF]")
PAGES = {"/": "Dashboard", "/approvals": "Approval desk", "/evaluation": "Evaluation"}
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "static")


class UiShellTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WS.DEMO = WS.Demo(None, "mock")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), WS.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True); cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close(); cls.thread.join(timeout=5)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def test_shared_nav_marks_the_current_page(self):
        for path, label in PAGES.items():
            st, _, body = self.get(path); html = body.decode()
            self.assertEqual(st, 200, path)
            nav = re.search(r'<nav class="app-nav".*?</nav>', html, re.S)
            self.assertIsNotNone(nav, path)
            links = re.findall(r'<a href="([^"]+)"([^>]*)>.*?<span>([^<]+)</span>', nav.group(0), re.S)
            self.assertEqual([l[2] for l in links], ["Dashboard", "Approval desk", "Evaluation"], path)
            current = [l[2] for l in links if 'aria-current="page"' in l[1]]
            self.assertEqual(current, [label], path)
            self.assertIn('class="skip-link"', html, path)

    def test_share_metadata_and_icons(self):
        for path in PAGES:
            html = self.get(path)[2].decode()
            for needle in ('name="description"', 'property="og:title"', 'property="og:description"',
                           'property="og:image"', 'name="twitter:card"', 'rel="icon"', 'name="theme-color"'):
                self.assertIn(needle, html, f"{path} missing {needle}")
            self.assertIsNone(EMOJI_RE.search(html), f"{path} contains an emoji")
        for asset, ctype in (("/static/favicon.svg", "image/svg+xml"), ("/favicon.ico", "image/svg+xml"),
                             ("/static/og-card.png", "image/png"), ("/static/apple-touch-icon.png", "image/png")):
            st, ct, body = self.get(asset)
            self.assertEqual(st, 200, asset); self.assertIn(ctype, ct, asset); self.assertGreater(len(body), 100, asset)

    def test_og_card_is_1200x630(self):
        with open(os.path.join(STATIC, "og-card.png"), "rb") as f:
            head = f.read(24)
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual((int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")), (1200, 630))

    def test_one_icon_library(self):
        html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
        self.assertIn("Lucide", html)
        sprite = open(os.path.join(STATIC, "icons", "lucide-sprite.svg"), encoding="utf-8").read()
        for page in ("approvals.html", "evaluation.html"):
            refs = re.findall(r'href="([^"]*#[^"]+)"', open(os.path.join(STATIC, page), encoding="utf-8").read())
            for ref in refs:
                if ref.startswith("#"): continue          # in-page anchors (skip links)
                self.assertTrue(ref.startswith("/static/icons/lucide-sprite.svg#lucide-"), ref)
                self.assertIn(f'id="{ref.split("#")[1]}"', sprite, ref)
        for name in re.findall(r'icon\("([a-z0-9-]+)"', open(os.path.join(STATIC, "approvals.js"), encoding="utf-8").read()):
            self.assertIn(f'id="lucide-{name}"', sprite, name)

    def test_first_run_guide_points_to_the_three_steps(self):
        html = self.get("/")[2].decode()
        guide = re.search(r'<section class="card start-card" id="start-here".*?</section>', html, re.S).group(0)
        self.assertIn('id="start-replay"', guide)
        self.assertIn('href="/approvals?demo=1"', guide)
        self.assertIn('href="/evaluation"', guide)
        self.assertIn("91 of 96", guide)

    def test_unknown_page_gets_a_friendly_html_404(self):
        st, ct, body = self.get("/does-not-exist")
        self.assertEqual(st, 404); self.assertIn("text/html", ct)
        self.assertIn(b"Page not found", body); self.assertIn(b'href="/"', body)
        st, ct, _ = self.get("/api/nope")
        self.assertEqual(st, 404); self.assertIn("application/json", ct)
        st, _, _ = self.get("/static/../server.py")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
