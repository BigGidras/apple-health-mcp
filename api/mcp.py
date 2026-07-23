"""
MCP Server for health data.
Exposes health metrics to Claude via Model Context Protocol.
"""
from http.server import BaseHTTPRequestHandler
from upstash_redis import Redis
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Vercel runtimes are >=3.9
    ZoneInfo = None
from urllib.parse import urlparse, parse_qs
import hmac
import json
import os

MCP_SECRET = os.environ.get("MCP_SECRET", "")
TIMEZONE = os.environ.get("TIMEZONE", "UTC")
EXERCISE_DAYS_PER_WEEK = os.environ.get("EXERCISE_DAYS_PER_WEEK", "")
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
    without this, 'today'/'yesterday' can land on the wrong calendar day for
    users outside UTC."""
    if ZoneInfo is None:
        return datetime.utcnow()
    try:
        return datetime.now(ZoneInfo(TIMEZONE))
    except Exception:
        return datetime.now(ZoneInfo("UTC"))


def clamp_days(days: int, maximum: int = MAX_DAYS) -> int:
    return max(1, min(days, maximum))


def check_secret(path: str) -> bool:
    """Fail-closed: refuses every request if MCP_SECRET isn't configured.
    Whitespace inside the provided key is stripped - hex keys never contain
    spaces, so any are copy/paste artifacts (seen as %20%20%20 in real logs)."""
    if not MCP_SECRET:
        return False
    query = parse_qs(urlparse(path).query)
    provided = "".join(query.get("key", [""])[0].split())
    return hmac.compare_digest(provided, MCP_SECRET)


def parse_exercise_routine() -> dict:
    """Parse exercise routine from env var."""
    if not EXERCISE_DAYS_PER_WEEK:
        return {}
    routine = {}
    for item in EXERCISE_DAYS_PER_WEEK.split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            try:
                routine[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return routine


def get_health_data(date_key: str) -> dict:
    raw = redis.hgetall(f"health:{date_key}") or {}
    return {field: json.loads(value) for field, value in raw.items()}


def get_cumulative_total(metric_data: dict) -> int:
    """Extract total from cumulative metric. Handles both storage formats."""
    if not metric_data:
        return 0
    if "total" in metric_data:
        return metric_data["total"]
    if "avg" in metric_data and "count" in metric_data:
        return round(metric_data["avg"] * metric_data["count"])
    return 0


def get_exercise_key(data: dict) -> str:
    """Handle iOS Shortcut naming quirk. Some configs have trailing space."""
    if "exercise " in data:
        return "exercise "
    return "exercise"


def extract_day_metrics(data: dict) -> dict:
    """
    Extract all health metrics from a day's data.
    Single source of truth for field extraction across all tools.
    """
    if not data:
        return None

    metrics = {}

    # HRV
    if "hrv" in data and data["hrv"].get("avg") is not None:
        metrics["hrv"] = round(data["hrv"]["avg"], 1)

    # Heart rate
    if "heartRate" in data:
        hr = data["heartRate"]
        if hr.get("min") is not None:
            metrics["resting_hr"] = round(hr["min"], 1)
        if "hr_zones" in hr and hr["hr_zones"].get("zone_pct"):
            metrics["hr_zones"] = hr["hr_zones"]["zone_pct"]

    # Prefer a dedicated resting-HR reading (e.g. from Health Auto Export) over
    # the derived min-of-samples value above, when both are present.
    if "restingHR" in data and data["restingHR"].get("avg") is not None:
        metrics["resting_hr"] = round(data["restingHR"]["avg"], 1)

    # Sleep
    if "sleep" in data:
        sleep = data["sleep"]
        if sleep.get("unrecognized"):
            metrics["sleep"] = {"quality": None, "note": "sleep stages not recognized in raw samples"}
        else:
            metrics["sleep"] = {
                "quality": sleep.get("quality"),
                "fragmentation_pct": sleep.get("fragmentation_pct"),
                "has_deep": sleep.get("has_deep"),
                "has_rem": sleep.get("has_rem")
            }

    # Exercise minutes
    exercise_key = get_exercise_key(data)
    if exercise_key in data:
        metrics["exercise_min"] = get_cumulative_total(data[exercise_key])

    # Steps
    if "steps" in data:
        metrics["steps"] = get_cumulative_total(data["steps"])

    # Active calories
    if "activeEnergy" in data:
        metrics["active_calories"] = get_cumulative_total(data["activeEnergy"])

    # Mindful minutes
    if "mindful" in data:
        metrics["mindful_min"] = get_cumulative_total(data["mindful"])

    # Respiratory rate
    if "respRate" in data and data["respRate"].get("avg") is not None:
        metrics["respiratory_rate"] = round(data["respRate"]["avg"], 1)

    return metrics if metrics else None


def get_hrv_baseline(days: int = 14) -> dict:
    """Calculate HRV baseline from recent history."""
    days = clamp_days(days)
    hrv_values = []
    for i in range(1, days + 1):
        date = (local_now() - timedelta(days=i)).strftime("%Y-%m-%d")
        data = get_health_data(date)
        if data and "hrv" in data and data["hrv"].get("avg") is not None:
            hrv_values.append(data["hrv"]["avg"])
    if not hrv_values:
        return {"baseline": None, "days": 0}
    return {
        "baseline": round(sum(hrv_values) / len(hrv_values), 1),
        "days": len(hrv_values)
    }


# MCP Tools

def tool_get_today() -> str:
    """Get all raw health metrics for today."""
    date_key = local_now().strftime("%Y-%m-%d")
    data = get_health_data(date_key)
    if not data:
        return json.dumps({"error": "No data synced today. Run iOS shortcuts."})
    return json.dumps(data, indent=2)


def tool_get_trends(days: int = 7) -> str:
    """Get health metrics over multiple days."""
    days = clamp_days(days)
    results = {}
    for i in range(days):
        date = (local_now() - timedelta(days=i)).strftime("%Y-%m-%d")
        data = get_health_data(date)
        metrics = extract_day_metrics(data)
        if metrics:
            results[date] = metrics
    if not results:
        return json.dumps({"error": f"No data for last {days} days."})
    return json.dumps(results, indent=2)


def tool_get_recovery_status() -> str:
    """Get recovery status with baseline comparisons and recent history."""
    date_key = local_now().strftime("%Y-%m-%d")
    data = get_health_data(date_key)
    baseline = get_hrv_baseline()

    status = {
        "date": date_key,
        "weekly_routine": parse_exercise_routine() or None
    }

    # Today's metrics (if synced)
    today_metrics = extract_day_metrics(data)
    if today_metrics:
        status["today"] = today_metrics

        # Add HRV baseline comparison if available
        if "hrv" in today_metrics and baseline.get("baseline"):
            hrv = today_metrics["hrv"]
            status["hrv_vs_baseline"] = {
                "today": hrv,
                "baseline": baseline["baseline"],
                "baseline_days": baseline["days"],
                "pct_diff": round(((hrv - baseline["baseline"]) / baseline["baseline"]) * 100)
            }

    # Recent days for pattern analysis
    recent_days = {}
    for i in range(1, 4):
        day_key = (local_now() - timedelta(days=i)).strftime("%Y-%m-%d")
        day_data = get_health_data(day_key)
        metrics = extract_day_metrics(day_data)
        if metrics:
            recent_days[f"day_minus_{i}"] = metrics
    if recent_days:
        status["recent_days"] = recent_days

    return json.dumps(status, indent=2)


# Tool definitions for MCP

TOOLS = [
    {
        "name": "get_today",
        "description": "Get raw health data for today. Returns unprocessed data as stored.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "get_trends",
        "description": "Get health metrics over multiple days: HRV, resting HR, HR zones, sleep, exercise minutes, steps, active calories, mindful minutes, respiratory rate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Number of days (default 7, max 90)"}
            },
            "required": []
        }
    },
    {
        "name": "get_recovery_status",
        "description": "Get comprehensive recovery data: today's metrics (HRV, resting HR, HR zones, sleep, exercise, steps, calories, mindful minutes, respiratory rate) with HRV baseline comparison, plus last 3 days with full metrics for trend analysis. Includes weekly exercise routine.",
        "inputSchema": {"type": "object", "properties": {}, "required": []}
    }
]


def handle_tool_call(name: str, args: dict) -> str:
    if name == "get_today":
        return tool_get_today()
    elif name == "get_trends":
        return tool_get_trends(args.get("days", 7))
    elif name == "get_recovery_status":
        return tool_get_recovery_status()
    return json.dumps({"error": f"Unknown tool: {name}"})


class handler(BaseHTTPRequestHandler):
    def send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if not check_secret(self.path):
            self.send_json({"error": "unauthorized"}, 401)
            return
        self.send_json({
            "name": "health",
            "version": "1.0.0",
            "description": "Personal health data from Apple Watch via iOS Shortcuts / Health Auto Export",
            "tools": TOOLS
        })

    def do_POST(self):
        if not check_secret(self.path):
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
        body = json.loads(self.rfile.read(content_length).decode("utf-8"))

        method = body.get("method", "")
        req_id = body.get("id")

        if method == "initialize":
            self.send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "health", "version": "1.0.0"}
                }
            })
        elif method == "tools/list":
            self.send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS}
            })
        elif method == "tools/call":
            params = body.get("params", {})
            result = handle_tool_call(params.get("name", ""), params.get("arguments", {}))
            self.send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": result}]}
            })
        else:
            self.send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}
            })
