"""Append-only, hash-chained audit log (JSONL). Each entry commits to the previous entry's hash."""
import hashlib, json, time

class AuditLog:
    def __init__(self, path=None):
        self.path, self.entries = path, []

    def record(self, event, **data):
        prev = self.entries[-1]["hash"] if self.entries else "0" * 64
        e = {"seq": len(self.entries), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "event": event, "data": data, "prev": prev}
        e["hash"] = hashlib.sha256(json.dumps(e, sort_keys=True, default=str).encode()).hexdigest()
        self.entries.append(e)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, default=str) + "\n")
        return e

    def verify_chain(self):
        prev = "0" * 64
        for e in self.entries:
            body = {k: v for k, v in e.items() if k != "hash"}
            if e["prev"] != prev or hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest() != e["hash"]:
                return False
            prev = e["hash"]
        return True

    def events(self):
        return [e["event"] for e in self.entries]
