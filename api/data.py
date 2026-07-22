"""
Health data retrieval endpoint.
Simple read access to stored health data.
"""
from http.server import BaseHTTPRequestHandler
from upstash_redis import Redis
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Vercel runtimes are >=3.9
    ZoneInfo = None
from urllib.parse import parse_qs, urlparse
import hmac
import json
import os

API_KEY = os.environ.get("API_KEY", "")
TIMEZONE = os.environ.get("TIMEZONE", "UTC")
MAX_DAYS = 90


def resolve_redis_env():
    """Vercel's Upstash/KV marketplace integrations name the REST credentials
    differently depending on how the store was connected (UPSTASH_*, KV_*, or
    a custom prefix). Accept any of them."""
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    if url and token:
        return url, token
    for name in sorted(os.environ):
        value = os.environ[name]
        if not value:
            continue
        if not url and (name.endswith("UPSTASH_REDIS_REST_URL") or name.endswith("KV_REST_API_URL")):
            url = value
        if not token and "READ_ONLY" not in name and \
                (name.endswith("UPSTASH_REDIS_REST_TOKEN") or name.endswith("KV_REST_API_TOKEN")):
            token = value
    return url, token


def redis_env_names() -> list:
    """Names (never values) of redis-looking env vars, for config diagnostics."""
    return sorted(n for n in os.environ
                  if any(k in n.upper() for k in ("UPSTASH", "KV_", "REDIS")))


_redis_url, _redis_token = resolve_redis_env()
redis = Redis(url=_redis_url, token=_redis_token) if _redis_url and _redis_token else None


def local_now() -> datetime:
    """Current time in TIMEZONE (default UTC). Vercel functions run in UTC, so
    without this, dates can land on the wrong calendar day for users outside UTC."""
    if ZoneInfo is None:
        return datetime.utcnow()
    try:
        return datetime.now(ZoneInfo(TIMEZONE))
    except Exception:
        return datetime.now(ZoneInfo("UTC"))


def check_auth(headers) -> bool:
    """Fail-closed: refuses every request if API_KEY isn't configured."""
    if not API_KEY:
        return False
    auth = headers.get("Authorization", "")
    return hmac.compare_digest(auth, f"Bearer {API_KEY}")


class handler(BaseHTTPRequestHandler):
    def send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def do_GET(self):
        if not check_auth(self.headers):
            self.send_json({"error": "unauthorized"}, 401)
            return

        if redis is None:
            self.send_json({
                "error": "redis not configured",
                "hint": "no UPSTASH_REDIS_REST_URL/TOKEN or KV_REST_API_URL/TOKEN pair found in this environment",
                "redis_env_names_found": redis_env_names()
            }, 500)
            return

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            days = int(query.get("days", [7])[0])
        except ValueError:
            days = 7
        days = max(1, min(days, MAX_DAYS))

        results = {}
        for i in range(days):
            date = (local_now() - timedelta(days=i)).strftime("%Y-%m-%d")
            raw = redis.hgetall(f"health:{date}") or {}
            if raw:
                results[date] = {field: json.loads(value) for field, value in raw.items()}

        self.send_json(results)
