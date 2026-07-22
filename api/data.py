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

redis = Redis(
    url=os.environ.get("UPSTASH_REDIS_REST_URL"),
    token=os.environ.get("UPSTASH_REDIS_REST_TOKEN")
)


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
    def do_GET(self):
        if not check_auth(self.headers):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
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

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(results, indent=2).encode())
