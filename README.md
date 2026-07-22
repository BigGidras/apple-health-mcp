# Apple Health MCP Server

An MCP server that streams your Apple Health data to Claude. Say "GM" and get a personalized daily health briefing based on your HRV, sleep, heart rate zones, and training patterns.

## Why This Exists

I wanted Claude to understand my body. Not generic health advice. My HRV. My sleep. My training load. Every morning I say "GM" and Claude tells me how I'm actually doing and what I should do about it.

## The Stack

```
Apple Watch → iPhone Health App → iOS Shortcuts → Vercel → Upstash Redis → Claude (MCP)
```

**Why Vercel?** Native MCP support. Deploy a Python function, it speaks MCP. Done.

**Why Upstash Redis?** Serverless Redis with a REST API. Free tier is generous. No infrastructure to manage. Data stays yours.

**Why iOS Shortcuts?** No app to build. Shortcuts can query HealthKit and POST to an API. Janky? A little. Works? Perfectly.

## What You Get

Every morning, Claude gives you:

- **Recovery read**: HRV vs your 14-day baseline, sleep quality, resting HR trends
- **Training guidance**: Push, maintain, or back off based on your body's signals
- **Watch-outs**: Patterns worth monitoring (sleep fragmentation, elevated HR, skipped meditation)
- **Day outlook**: How you'll likely feel

Claude sees all your metrics: HRV, resting HR, HR zones, sleep (quality, fragmentation, deep/REM), exercise minutes, steps, active calories, mindful minutes, and respiratory rate.

<details>
<summary>Example Output</summary>

![Example Output](screenshots/example-output.jpg)

</details>

## Setup (15 minutes)

### 1. Upstash Redis

1. Go to [upstash.com](https://upstash.com), create account
2. Create a Redis database (free tier)
3. Grab the REST URL and token

### 2. Deploy to Vercel

1. Fork this repo
2. Go to [vercel.com/new](https://vercel.com/new), import your fork
3. Add these environment variables:

| Variable | What it is |
|----------|------------|
| `UPSTASH_REDIS_REST_URL` | From Upstash dashboard |
| `UPSTASH_REDIS_REST_TOKEN` | From Upstash dashboard |
| `API_KEY` | Required. Run `openssl rand -hex 32`. Endpoints refuse all requests if unset. |
| `MCP_SECRET` | Required. Run `openssl rand -hex 16`. Endpoint refuses all requests if unset. |
| `TIMEZONE` | Optional, defaults to `UTC`. Your IANA timezone (e.g. `America/Sao_Paulo`), so "today"/"yesterday" match your local calendar day instead of the server's UTC clock. |
| `EXERCISE_DAYS_PER_WEEK` | Optional. e.g., `strength:4,yoga:7,cardio:2` |

4. Deploy. That's it.

<details>
<summary>Vercel Environment Variables</summary>

![Vercel Env](screenshots/vercel-env.jpg)

</details>

### 3. iOS Shortcuts

You'll create individual shortcuts for each health metric, then a master shortcut that runs them all.

**Sub-shortcuts (one per metric):**

Each sub-shortcut does two things:
1. **Find Health Samples** - Query a specific metric for the last 24 hours
2. **Get Contents of URL** - POST to `https://your-app.vercel.app/api/ingest`
   - Method: POST
   - Headers: `Authorization: Bearer YOUR_API_KEY`
   - Body: Form encoded, with field name matching the metric

**Critical configuration:**

| Field name | Health metric | Group by: Day | Source Filter |
|------------|---------------|---------------|---------------|
| `steps` | Step Count | YES | Your Apple Watch |
| `exercise` | Exercise Minutes | YES | Your Apple Watch |
| `activeEnergy` | Active Energy | YES | Your Apple Watch |
| `hrv` | Heart Rate Variability | NO | None |
| `heartRate` | Heart Rate | NO | None |
| `respRate` | Respiratory Rate | NO | None |
| `sleep` | Sleep Analysis | NO | None |
| `mindful` | Mindful Minutes | NO | None |

**Why this matters:**

Cumulative metrics (steps, exercise, active energy) need special handling:
- **Group by: Day** aggregates readings into daily totals instead of individual samples
- **Source Filter** prevents double-counting. Without it, iOS returns readings from both iPhone and Apple Watch, inflating totals by ~20%

Discrete metrics (HRV, heart rate, respiratory rate) should NOT use grouping. They need individual samples for proper avg/min/max calculations.

<details>
<summary>Sub-shortcut Example</summary>

![Shortcut Example](screenshots/shortcut-example.jpg)

</details>

<details>
<summary>Steps Shortcut with Group by Day + Source Filter</summary>

![Steps Shortcut](screenshots/steps-shortcut.png)

</details>

**Master shortcut:**

Create a master shortcut that calls all your sub-shortcuts in sequence. This is what you'll run each morning.

<details>
<summary>Master Shortcut</summary>

![Master Shortcut](screenshots/master-shortcut.png)

</details>

**Automation (semi-automated):**

You can schedule the master shortcut to run daily. Go to Shortcuts → Automation → Create Personal Automation → Time of Day.

**Important limitation:** iOS requires confirmation for health data access. The automation will prompt you to tap "Run" each morning. It cannot run fully automatically. Your phone must be unlocked, and you cannot trigger it from your Apple Watch.

**Want to skip the daily tap?** This is an iOS restriction on Shortcuts specifically ("Find Health Samples" always forces a confirmation), not a limitation of this server. A real installed app like [Health Auto Export](https://www.healthyapps.dev/) requests HealthKit permission once, then syncs in the background via Apple's native `HKObserverQuery`/background-delivery APIs — no daily confirmation. `/api/ingest` accepts Health Auto Export's REST API JSON export directly (in addition to the Shortcuts form-encoded body): point its REST API automation at `https://your-app.vercel.app/api/ingest` with header `Authorization: Bearer YOUR_API_KEY`, on a daily schedule. The metric-name mapping (`HAE_METRIC_MAP` in `api/ingest.py`) was reverse-engineered from public examples, not an official schema. **Treat the parsed HAE numbers as unverified until you check them**: every JSON payload received is also stored raw under `_debug_raw_hae` for that day (even when metrics do map), specifically because a wrong-but-plausible mapping (e.g. cumulative metrics assuming Shortcuts' single-daily-value shape, sleep stages counted by occurrence instead of duration) would otherwise compute silently-wrong numbers with no error. Compare `_debug_raw_hae` against the computed metrics for a day or two before trusting `get_recovery_status` on HAE-sourced data, and adjust `HAE_METRIC_MAP`/`parse_hae_metrics` if the shapes don't match.

<details>
<summary>Automation Setup</summary>

![Automation List](screenshots/automation-list.png)
![Automation Detail](screenshots/automation-detail.png)

</details>

**One more thing:** Go to Settings → Shortcuts → Advanced → Enable "Allow Sharing Large Amounts of Data". Otherwise iOS blocks large health exports.

Run your shortcuts once. Check your data is flowing: `https://your-app.vercel.app/api/data?days=1` (add your API key as Bearer token).

### 4. Connect Claude

Add the MCP server in Claude's settings under Connectors:

```json
{
  "mcpServers": {
    "health": {
      "url": "https://your-app.vercel.app/api/mcp?key=YOUR_MCP_SECRET"
    }
  }
}
```

<details>
<summary>Claude MCP Connector</summary>

![Claude Connector](screenshots/claude-connector.jpg)

</details>

### 5. System Instructions

Add this to your Claude project. Customize the "ABOUT ME" section for you.

```
When I say "GM" or "good morning", check my health data and brief me on my day.

ABOUT ME:
- 40s, focused on longevity and consistent training
- I track HRV as my primary recovery signal
- I'd rather undertrain than overtrain

WHAT I NEED FROM YOU:
1. Recovery read: How is my body doing? Compare yesterday's metrics vs baseline.
2. Training guidance: What should I do today? Push, maintain, or back off?
3. Watch-outs: Anything I should avoid or pay attention to?
4. Day outlook: How will I likely feel? What can I expect?

HOW TO REASON:
- Yesterday's data is your primary source. My shortcut syncs the previous day's data each morning, so "today" will always be empty. Don't mention missing today data.
- Look at yesterday's HRV vs baseline (negative % = below normal, needs attention)
- Look at recent_days for my training cycle. Low exercise_min (<30) = rest day, high (>60) = training day. Missing days = phone locked, no sync.
- Look at hr_zones for training intensity. High "hard"/"max" percentage = intense session. High "rest" = easy day or no workout.
- Look at yesterday's sleep quality (fragmentation, deep sleep, REM)
- Look at resting HR trend (elevated = accumulated fatigue)
- Look at steps and active_calories for total daily load. High step counts (>8k) or calories (>800) on top of training means more recovery/nutrition needed.
- Look at mindful_min for stress management. Zero meditation during high training load amplifies fatigue.
- Correlate signals across recent_days. One bad metric isn't the story, patterns are.

Keep it tight. 6-8 lines max. No disclaimers. Be direct.
```

## How It Works

Claude gets raw data. No scores. No pre-computed "recovery status." Just numbers.

The magic is letting the LLM reason. It notices things like:
- "HRV down but you had two hard days, that's expected adaptation"
- "Resting HR crept up 6 bpm this week, watch for accumulated fatigue"
- "Sleep fragmentation high but architecture is fine, you're okay"

## MCP Tools

| Tool | What it does |
|------|--------------|
| `get_today` | Raw health data for today (unprocessed) |
| `get_trends` | Full metrics over multiple days (default 7 days) |
| `get_recovery_status` | Comprehensive view: HRV vs 14-day baseline, plus last 3 days with all metrics (HRV, resting HR, HR zones, sleep, exercise, steps, calories, mindful, respiratory rate) |

## Troubleshooting

**Steps/exercise showing wrong values?**
- Verify "Group by: Day" is enabled for cumulative metrics (steps, exercise, activeEnergy)
- Verify Source Filter is set to your Apple Watch name
- Without these settings, you get sample counts instead of totals, and double-counted values

**Heart rate showing absurdly high values (e.g., 26,000)?**
- You accidentally enabled "Group by: Day" on a discrete metric
- Remove grouping from HRV, heartRate, and respRate shortcuts

**Data not matching Apple Health exactly?**
- HRV may differ slightly. Apple shows specific readings, we average all samples.
- Steps/exercise should match exactly with correct shortcut configuration.

**"No data synced today" error?**
- This is expected. Your shortcuts sync yesterday's data. Use `get_recovery_status` which includes `recent_days`.

## Security

`API_KEY` protects ingest and `/api/data`, `MCP_SECRET` protects the MCP endpoint. Both are required — every endpoint fails closed (refuses all requests) if its secret isn't configured, and comparisons use `hmac.compare_digest` to avoid timing side-channels. If you leak a key, rotate it in Vercel and update your shortcuts/automation.

## Your Data

Everything lives in your Upstash Redis, one hash per day (`HSET health:YYYY-MM-DD <metric> <value>`), so concurrent writes from different metrics/shortcuts update independently instead of racing to overwrite a single blob. Nothing shared.

## Inspiration

Inspired by [whoop-mcp-server](https://github.com/yuridivonis/whoop-mcp-server) by Yuri Divonis. Similar idea, different data source. This one uses Apple Watch data via iOS Shortcuts instead of WHOOP's API. No subscription required.
