"""Shared helpers: read-only n8n API access and the issue fingerprint.

Env: N8N_URL (e.g. https://jkdxs.app.n8n.cloud), N8N_API_KEY.
"""
import json, os, re, subprocess, time

BASE = os.environ.get("N8N_URL", "").rstrip("/") + "/api/v1"
KEY = os.environ.get("N8N_API_KEY", "")


def get(path):
    """GET with backoff. curl instead of urllib: n8n Cloud resets bursts of TLS
    connections and curl recovers more reliably."""
    if not KEY or BASE == "/api/v1":
        raise SystemExit("Set N8N_URL and N8N_API_KEY")
    r = None
    for attempt in range(6):
        r = subprocess.run(["curl", "-sf", "--retry", "3", "--retry-all-errors",
                            "-H", f"X-N8N-API-KEY: {KEY}", BASE + path],
                           capture_output=True, timeout=180)
        if r.returncode == 0:
            return json.loads(r.stdout)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"n8n API {path} failed (curl exit {r.returncode})")


def paged(path, limit=50, max_pages=40):
    """Yield items across cursor pages. Keep `limit` small when includeData=true."""
    sep = "&" if "?" in path else "?"
    cursor = None
    for _ in range(max_pages):
        page = get(f"{path}{sep}limit={limit}" + (f"&cursor={cursor}" if cursor else ""))
        yield from page.get("data", [])
        cursor = page.get("nextCursor")
        if not cursor:
            break
        time.sleep(0.5)


def _fnv1a(s, seed):
    h = seed
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def normalise_message(msg):
    norm = re.sub(r"[0-9a-f]{8,}|\d+", "#", (msg or "").lower())
    return re.sub(r"\"[^\"]{0,80}\"", "\"…\"", norm)[:160]


def fingerprint(workflow_id, node, msg):
    """Same workflow + node + error text with ids/numbers stripped = same issue.
    Mirrors the JavaScript in n8n/error-relay.workflow.json exactly."""
    key = f"{workflow_id}|{node}|{normalise_message(msg)}"
    return f"{_fnv1a(key, 0x811C9DC5):08x}{_fnv1a(key, 0x01000193):08x}"[:12]
