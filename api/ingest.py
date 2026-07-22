"""
Health data ingestion endpoint.
Receives data from iOS Shortcuts (form-encoded) or Health Auto Export
(JSON) and stores it in Redis.
"""
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote
from upstash_redis import Redis
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Vercel runtimes are >=3.9
    ZoneInfo = None
import hmac
import json
import os

API_KEY = os.environ.get("API_KEY", "")
TIMEZONE = os.environ.get("TIMEZONE", "UTC")


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

# Health Auto Export has no published schema; this mapping is reverse-engineered
# from community examples. Confirm against a real captured payload (see the
# "unrecognized JSON shape" branch below) and adjust if field names differ.
HAE_METRIC_MAP = {
    "heart_rate": "heartRate",
    "heart_rate_variability": "hrv",
    "resting_heart_rate": "restingHR",
    "step_count": "steps",
    "apple_exercise_time": "exercise",
    "active_energy": "activeEnergy",
    "respiratory_rate": "respRate",
    "mindful_minutes": "mindful",
    "sleep_analysis": "sleep",
}


def local_now() -> datetime:
    """Current time in TIMEZONE (default UTC). Vercel functions run in UTC, so
    without this, 'yesterday' can land on the wrong calendar day for users
    outside UTC syncing near local midnight."""
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


def parse_values(raw: str) -> list:
    """Parse newline-separated values from iOS Shortcuts."""
    decoded = unquote(raw).replace("\r\n", "\n").replace("\r", "\n")
    values = []
    for v in decoded.split("\n"):
        v = v.strip()
        if v:
            try:
                values.append(float(v))
            except ValueError:
                values.append(v)
    return values


def parse_hae_metrics(payload: dict) -> dict:
    """Map a Health Auto Export JSON export into {internal_key: [raw values]}."""
    result = {}
    metrics = payload.get("data", {}).get("metrics", [])
    for metric in metrics:
        internal_key = HAE_METRIC_MAP.get(metric.get("name", ""))
        if not internal_key:
            continue
        values = []
        for point in metric.get("data", []):
            if internal_key == "sleep":
                stage = point.get("value")
                if stage:
                    values.append(str(stage))
            else:
                qty = point.get("qty")
                if qty is not None:
                    values.append(qty)
        if values:
            result[internal_key] = values
    return result


def compute_hr_zones(values: list) -> dict:
    """
    Calculate time spent in each heart rate zone.
    Zones based on typical training thresholds.
    """
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return {}

    zones = {
        "rest": 0,      # < 100 bpm
        "light": 0,     # 100-120 bpm (yoga, walking)
        "moderate": 0,  # 120-140 bpm (strength, easy cardio)
        "hard": 0,      # 140-160 bpm (tempo, harder cardio)
        "max": 0        # 160+ bpm (intervals, sprints)
    }

    for hr in nums:
        if hr < 100:
            zones["rest"] += 1
        elif hr < 120:
            zones["light"] += 1
        elif hr < 140:
            zones["moderate"] += 1
        elif hr < 160:
            zones["hard"] += 1
        else:
            zones["max"] += 1

    total = len(nums)
    return {
        "zones": zones,
        "zone_pct": {k: round(v / total * 100) for k, v in zones.items()},
        "training_load": zones["moderate"] + zones["hard"] + zones["max"],
        "high_intensity": zones["hard"] + zones["max"]
    }


def compute_sleep_stats(values: list) -> dict:
    """Analyze sleep stage distribution."""
    stages = {"REM": 0, "Core": 0, "Deep": 0, "Awake": 0, "InBed": 0}
    for v in values:
        if isinstance(v, str):
            if "REM" in v:
                stages["REM"] += 1
            elif "Core" in v or "Light" in v:
                stages["Core"] += 1
            elif "Deep" in v:
                stages["Deep"] += 1
            elif "Awake" in v or "Wake" in v:
                stages["Awake"] += 1
            elif "InBed" in v or "In Bed" in v:
                stages["InBed"] += 1

    asleep_total = stages["REM"] + stages["Core"] + stages["Deep"] + stages["Awake"]
    if asleep_total == 0:
        # Nothing matched a known sleep stage (e.g. only "In Bed" samples, or an
        # unrecognized string format). Flag it instead of silently returning a
        # different shape - callers must check "unrecognized" before reading
        # quality/fragmentation_pct.
        return {"values": values, "unrecognized": True}

    fragmentation = round(stages["Awake"] / asleep_total * 100, 1)
    quality = "good" if fragmentation < 20 and stages["REM"] > 0 and stages["Deep"] > 0 else \
              "fair" if fragmentation < 35 else "poor"

    return {
        "stages": stages,
        "fragmentation_pct": fragmentation,
        "quality": quality,
        "has_rem": stages["REM"] > 0,
        "has_deep": stages["Deep"] > 0
    }


def compute_stats(values: list, key: str = "") -> dict:
    """Compute statistics for health samples."""
    key_lower = key.lower().strip()

    if key_lower == "sleep":
        return compute_sleep_stats(values)

    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return {"count": len(values)}

    # Cumulative metrics: daily totals from Watch (filtered in iOS Shortcuts)
    # Steps, Exercise, Active Energy use "Group by: Day" + Watch source filter
    cumulative_metrics = ["steps", "exercise", "activeenergy"]

    if key_lower in cumulative_metrics:
        return {
            "total": round(max(nums)),
            "sources": len(nums)
        }

    # Discrete metrics: compute full statistics
    result = {
        "avg": round(sum(nums) / len(nums), 2),
        "min": round(min(nums), 2),
        "max": round(max(nums), 2),
        "count": len(nums)
    }

    # Add HR zones for heart rate data
    if key_lower == "heartrate":
        result["hr_zones"] = compute_hr_zones(nums)

    return result


class handler(BaseHTTPRequestHandler):
    def send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self):
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

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode("utf-8")
        content_type = self.headers.get("Content-Type", "")

        # Data synced is "last 1 day" = yesterday's data
        date_key = (local_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        redis_key = f"health:{date_key}"

        if "application/json" in content_type:
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
                return

            # HAE_METRIC_MAP is reverse-engineered, unconfirmed against a real
            # payload - metrics that DO map (e.g. cumulative totals via max(),
            # sleep stages via occurrence-count) can still compute wrong numbers
            # silently. Always keep the raw payload for inspection, not just on
            # a total mapping miss, so parsed values can be checked against it.
            redis.hset(redis_key, "_debug_raw_hae", json.dumps(payload))

            metric_values = parse_hae_metrics(payload)
            if not metric_values:
                self.send_json({
                    "ok": True,
                    "note": "unrecognized JSON shape, stored raw for inspection",
                    "top_level_keys": list(payload.keys())
                })
                return
        else:
            form_data = parse_qs(raw_body)
            metric_values = {
                key: parse_values(values[0] if values else "")
                for key, values in form_data.items()
            }

        for key, values in metric_values.items():
            stats = compute_stats(values, key)
            redis.hset(redis_key, key, json.dumps(stats))
        redis.hset(redis_key, "_updated", json.dumps(local_now().isoformat()))

        self.send_json({
            "ok": True,
            "date": date_key,
            "keys": list(metric_values.keys())
        })

    def do_GET(self):
        self.send_json({
            "endpoint": "ingest",
            "method": "POST",
            "description": "Receives health data from iOS Shortcuts (form-encoded) or Health Auto Export (JSON)"
        })
