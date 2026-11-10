"""Minimal OpenAI-compatible chat client (stdlib only).

Configuration (first match wins):
  * environment variables OPENAI_BASE_URL, OPENAI_API_KEY, LLM_MODEL
    (OPENAI_PUBLIC_BASE_URL is used when OPENAI_BASE_URL is unset, e.g. on a hosted deployment)
  * a dotenv-style file named by LLM_ENV_FILE (KEY=VALUE lines; never commit it)
  * ./.env in the project root (git-ignored; see .env.example)
No key -> get_llm() returns None and every caller uses its deterministic offline fallback.
"""
import json, os, re, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MODEL = "deepseek-v4.1-flash"

class LLMError(Exception): pass

def _read_env_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out

def load_config(env=None):
    env = dict(os.environ if env is None else env)
    files = [p for p in (env.get("LLM_ENV_FILE"), os.path.join(ROOT, ".env")) if p]
    merged = {}
    for p in reversed(files): merged.update(_read_env_file(p))
    merged.update({k: v for k, v in env.items() if k in ("OPENAI_BASE_URL", "OPENAI_PUBLIC_BASE_URL", "OPENAI_API_KEY", "LLM_MODEL") and v})
    base = merged.get("OPENAI_BASE_URL") or merged.get("OPENAI_PUBLIC_BASE_URL")
    if not merged.get("OPENAI_API_KEY") or not base: return None
    return {"base_url": base.rstrip("/"), "api_key": merged["OPENAI_API_KEY"],
            "model": merged.get("LLM_MODEL") or DEFAULT_MODEL}

def _extract_json(text):
    """Parse the first JSON object in a model reply (tolerates ```json fences / prose)."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m: text = m.group(1)
    try: return json.loads(text)
    except ValueError: pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{": depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try: return json.loads(text[start:i + 1])
                    except ValueError: break
        start = text.find("{", start + 1)
    raise LLMError("model reply contained no JSON object")

class OpenAICompatLLM:
    def __init__(self, base_url, api_key, model=DEFAULT_MODEL, timeout=90, retries=3):
        self.base_url, self._key, self.model, self.timeout, self.retries = base_url, api_key, model, timeout, retries
        self.calls = 0

    def __repr__(self): return f"OpenAICompatLLM(model={self.model!r})"   # never print the key

    def chat(self, messages, tools=None, temperature=0.0, max_tokens=1500):
        """One chat-completions call; returns the assistant message dict (content / tool_calls)."""
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools: body["tools"], body["tool_choice"] = tools, "auto"
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode(),
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "Authorization": "Bearer " + self._key,
                                                             "User-Agent": "curl/8.5.0"})
        last = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                self.calls += 1
                msg = data["choices"][0]["message"]
                return {"role": "assistant", "content": msg.get("content") or "",
                        "tool_calls": msg.get("tool_calls") or []}
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
                last = e
                if isinstance(e, urllib.error.HTTPError) and e.code in (400, 401, 403, 404): break
                if attempt == self.retries: break
                wait = 1.5 * (attempt + 1)
                if isinstance(e, urllib.error.HTTPError) and e.code == 429:      # shared gateway rate limit
                    try: wait = max(wait, float(e.headers.get("Retry-After") or 0))
                    except ValueError: pass
                    wait = max(wait, 8.0 * (attempt + 1))
                time.sleep(wait)
        raise LLMError(f"LLM call failed: {type(last).__name__}: {str(last)[:200]}")

    def complete_json(self, system, user, max_tokens=800):
        """Ask for a single JSON object and parse it. Raises LLMError on failure."""
        m = self.chat([{"role": "system", "content": system + "\nReply with ONE JSON object only."},
                       {"role": "user", "content": user}], max_tokens=max_tokens)
        return _extract_json(m["content"])

    def complete_text(self, system, user, max_tokens=600):
        m = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=max_tokens)
        if not m["content"].strip(): raise LLMError("empty reply")
        return m["content"].strip()

def get_llm(env=None):
    cfg = load_config(env)
    return OpenAICompatLLM(cfg["base_url"], cfg["api_key"], cfg["model"]) if cfg else None
