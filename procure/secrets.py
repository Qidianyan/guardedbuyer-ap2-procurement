"""Sandbox credentials from a git-ignored `.secrets` file (KEY=VALUE lines).

    PAYPAL_CLIENT_ID=...
    PAYPAL_CLIENT_SECRET=...
    PAYPAL_BASE_URL=https://api-m.sandbox.paypal.com     # optional

The file lives at the repo root (override with env PAYPAL_SECRETS_FILE). It is listed in .gitignore; see
.secrets.example. This project is sandbox-only: a live PayPal API host is refused.
"""
import os
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, ".secrets")
SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
LIVE_HOSTS = {"api-m.paypal.com", "api.paypal.com"}

def secrets_path(path=None):
    return path or os.environ.get("PAYPAL_SECRETS_FILE") or DEFAULT_PATH

def load_secrets(path=None) -> dict:
    """Parse KEY=VALUE lines; ignores blank lines and # comments; strips quotes. {} if the file is missing."""
    p = secrets_path(path)
    out = {}
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k:
                    out[k] = v
    except FileNotFoundError:
        return {}
    return out

def check_base_url(base_url):
    host = (urlsplit(base_url).hostname or "").lower()
    if host in LIVE_HOSTS:
        raise ValueError("live PayPal API host refused: this project only talks to the sandbox")
    return base_url.rstrip("/")

def paypal_credentials(path=None):
    """{"client_id", "client_secret", "base_url"} or None when id/secret are missing."""
    s = load_secrets(path)
    cid, sec = s.get("PAYPAL_CLIENT_ID"), s.get("PAYPAL_CLIENT_SECRET")
    if not cid or not sec:
        return None
    return {"client_id": cid, "client_secret": sec, "base_url": check_base_url(s.get("PAYPAL_BASE_URL") or SANDBOX_BASE)}

def mask(value) -> str:
    """Show at most the first 4 characters and the length; never the full value."""
    if not value:
        return "(empty)"
    v = str(value)
    return f"{v[:4]}…({len(v)} chars)" if len(v) > 8 else f"…({len(v)} chars)"
