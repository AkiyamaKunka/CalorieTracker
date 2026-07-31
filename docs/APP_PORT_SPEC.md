# CalorieTracker — Flutter App Port Specification

**Status**: authoritative implementation spec for the on-device Flutter (iOS + Android) port.
**Extracted from**: the production Python stack at commit `4f9020a` (main). Every rule cites `file:line`.
**Architecture delta**: the app keeps its DATA on one device — local SQLite, on-device photo
detection, no Telegram and no Termux watcher process. Where the Python stack defends against
multi-process races, this spec states the honest single-process simplification (with one exception:
the app runs a second isolate for background scans — see §9). The Python stack stays in production
unchanged.

**ANALYSIS ENGINES (updated 2026-07-26 — this section used to say "Gemini only")**: the app now
ships FOUR interchangeable implementations of the §3 analyzer seam, chosen by a Settings dropdown:
Gemini REST (default), OpenAI, Anthropic, and *the user's own server* — which forwards to
`/api/analyze_photo` on the Python stack and runs the Claude Code CLI under the owner's
subscription, i.e. the very `claude_analyzer.py` path the original delta said would not be ported.
Everything downstream of the model reply (coercion, retry/quota rules, persistence) is shared, and
the golden vectors pin it. Rules below written as "Gemini …" apply to whichever provider is active
unless they name an HTTP shape.

Historical note: the server optionally routes photos through a Claude CLI first
(`telegram_bot.py:1307-1314`, `claude_analyzer.py`). The `analyzed_by` provenance key
(`telegram_bot.py:1312`) may be written as
`"gemini"` for future auditability but nothing reads it.

---

## 1. PROMPTS

### 1.1 FOOD_DETECTION_PROMPT (`config.py:47-81`)

Send **verbatim** (subject only to §1.3 dietary-profile append):

```
Analyze this photo and determine if it contains food, a meal, or a beverage.

Beverages COUNT as food here: coffee (including plain black coffee), tea,
lattes, juice, soda, bubble tea, protein shakes, beer, and any other drink
a calorie tracker should log. Estimate their calories from what is visible
(cup size, milk foam, color). Only plain water counts as no calories — log
it as is_food false.

If the image contains NO food, meal, or beverage, respond with exactly:
{"is_food": false}

If the image DOES contain food, respond with a JSON object like this:
{
  "is_food": true,
  "food_items": [
    {"name": "Grilled Chicken Breast", "estimated_calories": 280, "protein_g": 43, "carbs_g": 0, "fat_g": 12},
    {"name": "Caesar Salad", "estimated_calories": 170, "protein_g": 7, "carbs_g": 12, "fat_g": 10}
  ],
  "total_calories": 450,
  "total_protein_g": 50,
  "total_carbs_g": 12,
  "total_fat_g": 22,
  "meal_description": "Grilled chicken breast with Caesar salad",
  "confidence_note": "Portions appear to be standard restaurant serving sizes"
}

Rules:
- Respond ONLY with valid JSON, no other text
- Estimate calories based on typical portion sizes visible in the photo
- For each food item, estimate protein (g), carbs (g), and fat (g)
- Include total_protein_g, total_carbs_g, total_fat_g as sums
- Be as specific as possible about the food items
- If you can see the portion size, adjust your estimate accordingly
- Include a brief confidence note about the estimate uncertainty
```

### 1.2 TEXT_HANDLER_PROMPT (`config.py:98-170`)

Stored as a Python `.format` template. `{{` / `}}` are **literal braces** in the output; the app
must produce the single-brace text below with the five placeholders substituted. Send verbatim
after substitution:

```
You are a calorie tracking assistant. The user sent a text message.

Date context (the user's local time): today is {today} ({weekday}); yesterday was {yesterday}.
Use this to resolve relative references like "yesterday", "this morning", or "last night" to the matching Date in the meals list below.

Here are the recently logged meals (each line shows its Date so you can match relative references):
{meals_list}

The user says: "{user_message}"

Your job is to determine the intent of the message and respond with JSON.
Intent can be:
1. "new_meal": The user is describing a new meal they ate.
2. "correction": The user is correcting an existing meal from the recent meals list (e.g., "change yesterday's lunch to 500 kcal").
3. "delete": The user wants to completely delete one or more meals (e.g., "delete all food today", "remove meal 0").
4. "chat": A general question, greeting, or unrelated message.
5. "log_weight": The user is reporting their body weight (e.g., "I weigh 72.5 kg this morning", "weighed 159 lb today").
6. "log_activity": The user is reporting exercise they did — calories burned, steps, and/or distance (e.g., "burned 450 calories on my 5 km run", "did 8000 steps today").

Respond with JSON ONLY in this exact format:
{
  "intent": "new_meal",
  "meal_index": 0,
  "meal_indices": [0],
  "reason": "Briefly explain your logic",
  "analysis": {
    "is_food": true,
    "food_items": [
      {"name": "Item Name", "estimated_calories": 000, "protein_g": 00, "carbs_g": 00, "fat_g": 00}
    ],
    "total_calories": 000,
    "total_protein_g": 00,
    "total_carbs_g": 00,
    "total_fat_g": 00,
    "meal_description": "...",
    "confidence_note": "..."
  },
  "weight_kg": 0,
  "active_calories": 0,
  "steps": 0,
  "distance_km": 0,
  "reply": "Friendly response to the user"
}

COMPOUND REQUESTS: if the user's single message contains MULTIPLE distinct requests
(e.g. "correct meal 2 to roast duck rice AND delete meal 3"), respond with ONE
JSON OBJECT (never a bare JSON array) in this shape instead:
{
  "intent": "multi",
  "actions": [
    { ...first request, same fields as a single response... },
    { ...second request... }
  ],
  "reply": "Brief summary of what you are doing"
}
Each entry in "actions" uses exactly the same schema as a single response above.
All "meal_index"/"meal_indices" values in every action refer to the ORIGINAL
meals_list shown above (indices never shift between actions). Combine ALL
deletions into ONE "delete" action listing every index in "meal_indices".
List actions in the order the user stated them. Use at most 5 actions.

Rules:
- Respond ONLY with valid JSON, no other text.
- Respond with a single JSON OBJECT at the top level — NEVER a bare JSON array. Multiple requests go inside "actions" of a "multi" object.
- For "new_meal", estimate calories based on standard portion sizes and return it in "analysis".
- For "correction", accurately identify the "meal_index" (the exact 0-based index from the provided `meals_list` array), apply the correction, and return the FULL updated "analysis".
- For "delete", accurately identify all targeted meals from `meals_list` and return their 0-based indices as a list in "meal_indices". Provide a brief "reason".
- For "chat", just return a friendly "reply" string.
- For "log_weight", set "weight_kg" to the body weight in kilograms (convert pounds to kg). Do NOT treat food as weight.
- For "log_activity", set "active_calories" (calories burned), "steps", and "distance_km" from the message; use 0 for anything not stated.
- A message describing FOOD the user ate is always "new_meal", never "log_weight" or "log_activity".
- If the user describes food but meals_list is empty, it MUST be a "new_meal", not a "correction" or "delete".
```

**Placeholders the app must fill** (`telegram_bot.py:4285-4293`):

| Placeholder | Value |
|---|---|
| `{today}` | user-local today, ISO `YYYY-MM-DD` |
| `{weekday}` | English weekday name of today (`strftime("%A")`, e.g. `Thursday`) |
| `{yesterday}` | user-local today − 1 day, ISO |
| `{meals_list}` | see below |
| `{user_message}` | the raw user text |

`meals_list` format (`telegram_bot.py:4265-4281`): `"No meals logged recently."` when the recent
**food** meal snapshot (last `TEXT_EDIT_WINDOW_DAYS` = 7 calendar days including today, is_food only,
ordered by timestamp ascending — `telegram_bot.py:4263`, `845-848`; `database.py:439-444`) is empty.
Otherwise one line per meal, `\n`-joined:

```
[{i}] Date: {date} | Meal: {meal_description} (~{total_calories} kcal) — Items: {name1}, {name2}
```

where `i` is the 0-based index into the snapshot, `total_calories` is the **raw** stored value
(defaulting to 0 if missing), and Items come from `safe_food_items` (§3.5) with each item's `name`
stringified (missing → `"?"`). Stored analyses are untrusted; a poison row must not break prompt
building (`telegram_bot.py:4272-4277`).

### 1.3 Dietary-profile append hook (`config.py:83-91`)

If the user has a non-blank dietary profile text (app: a settings field replacing
`~/CalorieTracker/dietary_profile.txt`), append to **FOOD_DETECTION_PROMPT only**:

```
\n\nUser's Dietary Profile / Cultural Context:\n{profile_text}\nPlease strongly consider these preferences when analyzing the photo.\n
```

The text handler prompt gets no append.

---

## 2. DATA MODEL (SQLite)

DDL is created idempotently on startup (`database.py:20-156`), WAL journal mode
(`database.py:25`), foreign keys ON (`database.py:16`). Port the tables verbatim. `chat_id` exists
in every table for multi-user safety on the server; the app keeps the column and uses one constant
local user id (e.g. `1`) everywhere.

### 2.1 Tables

```sql
-- database.py:26-43
CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    date TEXT NOT NULL,          -- USER-LOCAL calendar day, ISO YYYY-MM-DD
    time TEXT NOT NULL,          -- USER-LOCAL clock time, strftime("%I:%M %p") e.g. "07:42 PM"
    timestamp TEXT NOT NULL,     -- server-clock datetime.now().isoformat()
    source TEXT,                 -- 'telegram' | 'api_auto' | 'manual_text' | ... (app: e.g. 'camera','gallery','manual_text')
    image_hash TEXT,             -- normalized md5 hex of ORIGINAL photo bytes; '' for text meals
    file_id TEXT,                -- Telegram file id; app: local photo path/uri or ''
    analysis TEXT NOT NULL,      -- the analysis dict as JSON text (json.dumps)
    corrected BOOLEAN DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chat_date ON meals(chat_id, date);
CREATE INDEX IF NOT EXISTS idx_chat_hash ON meals(chat_id, image_hash);

-- database.py:53-68
CREATE TABLE IF NOT EXISTS photo_ingestions (
    chat_id INTEGER NOT NULL,
    image_hash TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'processing',
    meal_id INTEGER,
    PRIMARY KEY (chat_id, image_hash)
);
CREATE INDEX IF NOT EXISTS idx_photo_ingestions_status ON photo_ingestions(chat_id, status);

-- database.py:45-51 — server-only concept (watcher liveness + device timezone).
-- App: OMIT, or keep as a stub. See §2.3 clock simplification.
CREATE TABLE IF NOT EXISTS heartbeats (
    device_name TEXT PRIMARY KEY,
    last_ping_time TEXT NOT NULL,
    timezone TEXT DEFAULT '+0800'
);

-- database.py:72-83 — one canonical weigh-in per user-local day; re-log overwrites (upsert
-- ON CONFLICT(chat_id,date), database.py:744-762)
CREATE TABLE IF NOT EXISTS body_weight (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    source TEXT DEFAULT 'manual',
    note TEXT DEFAULT '',
    logged_at TEXT NOT NULL,
    UNIQUE(chat_id, date)
);

-- database.py:86-100 — append-only, multiple per day allowed
CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    workout_type TEXT DEFAULT 'strength',
    muscle_groups TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    duration_min REAL,
    source TEXT DEFAULT 'manual',
    details TEXT,                -- JSON text or NULL
    logged_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workouts_chat_date ON workouts(chat_id, date);

-- database.py:104-124 — upsert by (chat_id, source, external_id); manual rows have
-- external_id NULL and always insert (SQLite treats NULLs as distinct in UNIQUE),
-- database.py:819-854
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    activity_type TEXT DEFAULT '',
    source TEXT DEFAULT 'manual',
    active_calories REAL,
    distance_km REAL,
    duration_min REAL,
    avg_hr_bpm INTEGER,
    elevation_gain_m REAL,
    start_time TEXT,
    external_id TEXT,
    notes TEXT DEFAULT '',
    raw TEXT,                    -- JSON text; manual NL steps ride here as {"steps": N}
    logged_at TEXT NOT NULL,
    UNIQUE(chat_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_activities_chat_date ON activities(chat_id, date);

-- database.py:127-148 — single config row per user
CREATE TABLE IF NOT EXISTS fitness_profile (
    chat_id INTEGER PRIMARY KEY,
    diet_mode TEXT,
    target_calories INTEGER,
    target_protein_g INTEGER,
    target_carbs_g INTEGER,
    target_fat_g INTEGER,
    protein_g_per_kg REAL,
    goal_weight_kg REAL,
    height_cm REAL,
    vdot REAL,
    race_distance_km REAL,
    race_time_seconds INTEGER,
    race_label TEXT,
    goal_race_date TEXT,
    plan_start_date TEXT,
    long_run_day INTEGER DEFAULT 6,   -- Monday=0..Sunday=6
    extra TEXT,                        -- JSON text
    updated_at TEXT
);
```

### 2.2 Semantics that matter

- **`analysis` is JSON text.** Written with `json.dumps(analysis)` (`database.py:180`). On read,
  parse it; if the parse result is **not an object/map**, coerce to `{}` — never crash a reader on
  a stored `null`/string/array literal (`database.py:428-437`). App equivalent: every meal loader
  returns `Map<String, dynamic>`, coercing non-map JSON to `{}`.
- **`corrected`** is stored 0/1, read back as bool (`database.py:435`). Set to 1 by any correction
  write (`database.py:567-576` sets `corrected = 1` unconditionally in `update_meal_analysis`).
  Views render a ✏️ marker on corrected meals (`telegram_bot.py:3917`, `daily_report.py:397`), and
  the daily report suppresses the calorie-mismatch flag on corrected meals (`daily_report.py:421`).
- **`date`/`time` vs `timestamp`.** On the server, `date` and `time` come from the *user-local*
  clock (device-reported timezone offset via heartbeats — `database.py:700-715`,
  `telegram_bot.py:1809-1815`) while `timestamp` is the *server* clock, used for duplicate-window
  and staleness math. **App simplification (documented, deliberate)**: there is one clock — the
  device's. `date` = local calendar day, `time` = local `hh:mm AM/PM`, `timestamp` = local
  `DateTime.now().toIso8601String()`. All `user_local_now()`/`user_local_today()` call sites
  become plain local now/today; the heartbeats table and `parse_timezone_offset`
  (`database.py:684-697`) have no app role.
- **Backdated meals**: when a `captured_at` (photo capture moment) is known and valid (§6.4),
  `date`/`time` derive from it instead of now; `timestamp` stays now
  (`telegram_bot.py:1798-1817`).
- **`image_hash` normalization** (`database.py:192-193`): every hash is passed through
  `str(hash or "").strip().lower()` before **any** write or lookup. Dart:
  `(hash ?? '').trim().toLowerCase()`. Empty-after-normalization hash means "no hash": ledger
  writes and reservations are skipped/no-ops (`database.py:170`, `217-218`, `295-296`).
- **Time format**: `time` is `strftime("%I:%M %p")` — 12-hour, zero-padded, space, AM/PM
  (`telegram_bot.py:1814`).

### 2.3 photo_ingestions: statuses and the reservation/dedup contract

Statuses in production (writes cited):

| status | meaning | written at |
|---|---|---|
| `processing` | reserved, analysis in flight | `database.py:263`, reclaim `250` |
| `saved` | meal row exists (`meal_id` set) | atomic with meal INSERT, `database.py:182-184` via `save_meal(mark_status="saved")` (`telegram_bot.py:4540-4541`, `5029-5030`) |
| `skipped` | analyzed, not food | `telegram_bot.py:4575`, `5048` |
| `failed` | analysis failed, original kept for retry | `telegram_bot.py:4507`, `5003` |
| `deleted` | tombstone: meal deleted, or failed upload cleared — suppresses auto re-log while allowing deliberate re-send | `database.py:592-596` (delete_meal), `database.py:318-339` (clear-failed) |

**`reserve_photo_hash` contract** (`database.py:205-268`) — the app must reproduce this decision
tree (in one SQLite transaction; on-device a synchronous critical section suffices):

1. Normalize hash; empty → return true (nothing to reserve).
2. **Meals-table backstop**: if any meal row already has this `(chat_id, image_hash)` → refuse
   (`database.py:226-232`). This catches ledger/meal divergence.
3. If a ledger row exists:
   - reclaim and return true (row → `status='processing'`, `meal_id=NULL`, `last_seen_at=now`,
     `source` updated) when **either** (a) `status='processing'` and the row is stale —
     `last_seen_at` older than `PHOTO_RESERVATION_STALE_SECONDS` = 6 h (`database.py:11`,
     `196-202`, `242-254`), or (b) `status ∈ reclaim_statuses` passed by the caller;
   - otherwise refuse.
4. No row: INSERT `(status='processing', first_seen_at=last_seen_at=now)`; success = the insert
   landed (`database.py:259-265`).

**Caller policies** (port exactly):

- Deliberate user share/re-send of a photo (server: Telegram chat photo):
  `reclaim_statuses={"failed", "skipped", "deleted"}` — a human re-sending a photo may re-log
  something previously skipped/failed/deleted (`telegram_bot.py:4669-4674`).
- Automated ingestion (server: `/upload` from watcher; app: the auto camera-roll intake of §6):
  **no** reclaim statuses — strict (`telegram_bot.py:4882`).
- Manual retry of a failed item: `reclaim_statuses={"failed"}` (`telegram_bot.py:2077-2081`).

**Status transitions the pipeline must guarantee** (server: `telegram_bot.py:4457-4591`):
every reservation ends as `saved`/`skipped`/`failed`, or the `processing` row is **deleted**
(`release_photo_hash`, `database.py:304-315` — deletes only rows still in `processing`) when
nothing was kept. The meal INSERT and the `saved` mark are one transaction
(`database.py:158-189`).

**Additional dedup layer** (pre-reservation, chat path only): "same photo again within 5 minutes"
— if a meal from *today* has the same hash and its `timestamp` is `< DUPLICATE_WINDOW_MINUTES`
(= 5, `config.py:95`) old, tell the user it's a duplicate before even trying to reserve
(`telegram_bot.py:2854-2872`). A meal with the hash but a missing/blank timestamp counts as a
duplicate; an unparseable timestamp does not.

**App simplification (honest)**: the server contract defends against overlapping bot processes,
Android queue retries, and crash-recovery sweeps. A single-process app needs none of the thread/
crash races — but keeps: (1) dedup by md5 of **original** bytes, (2) the status ledger with the
five statuses above and the `deleted` tombstone (so the §6 backfill scan never re-logs a deleted
or skipped photo), (3) the meals-table backstop check, (4) the reclaim-statuses distinction
between user-initiated and automated intake. The 6-hour stale-processing reclaim degenerates to:
on app launch, any `processing` row is a crashed run — reclaim it (or mark `failed`) immediately;
no timer needed.

### 2.4 Meal mutation semantics

- `update_meal_analysis(meal_id, chat_id, new_analysis)`: rewrites `analysis` JSON, sets
  `corrected=1`, keyed by DB id + chat id (`database.py:567-576`).
- `delete_meal(meal_id, chat_id)`: deletes the row; if it had an image_hash, the ledger row is
  updated to `status='deleted'`, `meal_id=NULL` (`database.py:578-597`) — this is what stops the
  backfill scan from resurrecting the photo.

---

## 3. ANALYSIS PIPELINE

### 3.1 Image normalization — exact production parameters

`_normalize_photo_for_analysis` (`telegram_bot.py:1135-1161`). One decode per photo; the result
feeds the model call *and* any UI echo. Parameters, in order:

1. Decode header; compute `scale = 1568 / max(width, height)`.
2. If `0 < scale < 1`, request a **draft** (JPEG DCT-domain reduced-scale) decode at the
   **aspect-corrected** box `(round(width*scale), round(height*scale))`, each dim min 1 —
   rationale: draft only engages when *both* dims of the request are ≤ the eventual size, so a
   square 1568×1568 box on a 4:3 image never engages it (`telegram_bot.py:1142-1144`).
3. Apply EXIF orientation transpose (bake rotation in).
4. `thumbnail((1568, 1568))` — downscale so the long side ≤ 1568 px, aspect preserved, never
   upscale.
5. Encode RGB JPEG **quality 85**.
6. Any failure → return null and fall back to the original bytes (`telegram_bot.py:1159-1161`).

Measured rationale (docstring, `telegram_bot.py:1136-1146`): 1568 px matches the Anthropic
server-side cap (accuracy-neutral) and is comfortably above Gemini's needs; single-decode replaced
three full-size decodes as the biggest RAM spike on a 1 GB host. In Dart use `package:image` or
platform codecs; the draft() step is a JPEG-decoder optimization, not a correctness requirement —
required outputs are: EXIF-upright, ≤1568 px long side, JPEG q85.

(The legacy fallback path `_prepare_image_for_gemini`, 1024 px thumbnail with no re-encode,
`telegram_bot.py:1164-1180`, only runs when normalization failed; the app may simply send original
bytes in that case.)

### 3.2 Gemini call shape (REST, user's own key)

Production uses the `google-genai` SDK with `GenerateContentConfig(response_mime_type=
"application/json")` (`GEMINI_JSON_CONFIG`, `telegram_bot.py:129`) and model `GEMINI_MODEL`
(default `gemini-2.5-flash`, `config.py:26`). Photo calls send `[FOOD_DETECTION_PROMPT,
image_part]` where the normalized JPEG rides as an inline bytes part with mime `image/jpeg`
(`telegram_bot.py:1255-1267`). Text calls send `[prompt]` with the same JSON config
(`telegram_bot.py:4301-4306`).

App REST equivalent — `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={API_KEY}`:

```json
{
  "contents": [{
    "parts": [
      {"text": "<FOOD_DETECTION_PROMPT (+ dietary profile)>"},
      {"inline_data": {"mime_type": "image/jpeg", "data": "<base64 of normalized JPEG>"}}
    ]
  }],
  "generationConfig": {"response_mime_type": "application/json"}
}
```

Text-handler call: same, with a single text part. Extract
`candidates[0].content.parts[0].text` and feed it to the JSON parser (§3.4).

Bound every call with a hard client-side deadline of **90 s**
(`GEMINI_HTTP_DEADLINE_SECONDS`, `telegram_bot.py:154`, enforced in
`_generate_content_with_deadline`, `telegram_bot.py:395-427`); a deadline hit is treated as a
retryable network error.

### 3.3 Retry / backoff / quota pause (photo path)

Port `analyze_food_photo_with_retries` (`telegram_bot.py:1286-1380`) in simplified form:

- Up to `GEMINI_ANALYSIS_MAX_ATTEMPTS` = 3 attempts (`telegram_bot.py:122`) for **background/auto**
  intake; the interactive share path uses **1 attempt** (`analyze_food_photo`,
  `telegram_bot.py:1271-1283`) — fail fast, the user is watching.
- Error classification (`_classify_gemini_error`, `telegram_bot.py:886-903`) by matching the
  upper-cased error text:
  - JSON parse failure → `parse_error` (do **not** retry: `telegram_bot.py:1338-1346`).
  - daily free-tier quota (`_is_daily_free_tier_quota_error`, `telegram_bot.py:906-915`: text
    contains `GENERATEREQUESTSPERDAYPERPROJECTPERMODEL-FREETIER`, or
    `GENERATE_CONTENT_FREE_TIER_REQUESTS` together with `PERDAY`/`PER DAY`/`LIMIT: 20`) →
    `daily_quota_exhausted`.
  - `RESOURCE_EXHAUSTED`/`429`/`QUOTA`/`RATE_LIMIT` → `quota_rate_limit`;
    `API_KEY`/`UNAUTHENTICATED`/`PERMISSION_DENIED`/`401`/`403` → `auth`;
    `UNAVAILABLE`/`DEADLINE`/`TIMEOUT`/`CONNECTION`/`503` → `network_service`;
    `INVALID_ARGUMENT`/`MODEL_NOT_FOUND`/`NOT_FOUND` → `model_error`; else `unknown`.
- **Retryable** = `RESOURCE_EXHAUSTED`/`429`/`UNAVAILABLE`/`503` and not daily-quota
  (`telegram_bot.py:872-883`). Retry delay: parse the server's `retryDelay: "Ns"` / `retry in Ns` /
  `retry in Nms` hint from the error body, ceil to int, clamp to
  [1, `GEMINI_RETRY_MAX_DELAY_SECONDS`=60]; default `GEMINI_RETRY_BASE_DELAY_SECONDS`=5
  (`telegram_bot.py:852-869`, `123-124`).
- **Daily-quota pause UX** (worth porting): on `daily_quota_exhausted`, record a pause-until
  timestamp = now + `GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS` (default 12 h, `telegram_bot.py:125`,
  `978-988`). While paused: skip photo analysis entirely (`telegram_bot.py:1316-1319`) and refuse
  NL text requests with an explanation showing pause-until and time remaining
  (`telegram_bot.py:4253-4261`, summary format `951-966`). Photos arriving during a pause are kept
  with status `failed` and the user is offered keep-for-retry vs discard
  (`telegram_bot.py:4500-4527`). Any later Gemini success clears the pause
  (`telegram_bot.py:1003-1008`). App: persist `quota_pause_until` in settings storage; offer a
  "retry failed photos" action.

### 3.4 Response JSON parsing (`utils.py:15-36`)

`parse_ai_json`: strip; if it starts with ``` ``` ``` remove the opening fence (optionally
`json`-tagged) and the closing fence; try full-string JSON parse; on failure find the earliest of
`{`/`[` and the latest of `}`/`]` and parse that slice; if the slice is invalid or absent,
propagate the parse error (→ `parse_error`, no retry).

### 3.5 The analysis contract and EVERY coercion rule

Nominal analysis object: `is_food` (bool), `food_items` (list of `{name, estimated_calories,
protein_g, carbs_g, fat_g}`), `total_calories`, `total_protein_g`, `total_carbs_g`, `total_fat_g`,
`meal_description`, `confidence_note`. **Gemini's JSON mode guarantees syntax, not schema** — the
consumers never trust shapes:

- **`safe_number(value, default=0)`** (`utils.py:39-57`): returns the value only if it is an
  `int`/`double` that is **not a bool** and lies **strictly** inside `(-1e9, 1e9)` (this also
  rejects NaN and ±Infinity); anything else → default. Used for every arithmetic read of calories/
  macros. In Dart, note JSON huge-literals: mirror the semantics (non-numeric → default; check
  `value.isFinite && value > -1e9 && value < 1e9`).
- **`safe_food_items(analysis)`** (`utils.py:60-72`): non-map analysis → `[]`; `food_items`
  missing or not a list → `[]`; otherwise keep **only** the entries that are maps. All renderers
  and prompt builders iterate this, never the raw field.
- **`is_food` truthiness**: consumers use raw truthiness, not `== true` — filtering is
  `analysis.get("is_food")` truthy (`telegram_bot.py:842`, `daily_report.py:360`), so `1`,
  `"yes"`, non-empty strings/arrays count as food; `false`/`0`/`""`/`null`/missing do not. The
  SQL stats aggregate spells out exactly this Python-truthiness across every JSON type
  (`database.py:453-461`). Dart: implement one `isFoodTruthy(dynamic)` helper reproducing Python
  truthiness (false, 0, 0.0, "", [], {}, null → false; else true) and use it everywhere.
  Exception: the macro analyzer skips only entries where `is_food` is **literally false**
  (`nutrition.py:172`).
- **`parse_boolish(value)`** (`utils.py:100-110`): tri-state settings parser —
  `{"1","true","yes","y","on"}` → true, `{"0","false","no","n","off"}` → false (case/whitespace
  insensitive, via `str(value)`), else null. Used for env-style knobs, not analysis fields.
- **`meal_calorie_mismatch(analysis)`** (`utils.py:75-97`): sum `estimated_calories` over
  `safe_food_items` counting only values that pass the `_as_number` bounds; if no items counted,
  the sum ≤ 0, or `total_calories` isn't a bounded number → null (consistent). Flag when
  `|total − item_sum| > max(100, 0.2 * max(total, item_sum))`; return `int(item_sum)`.
- **Persisting corrections/new meals**: if `food_items` is present in a model-supplied analysis,
  replace it with `safe_food_items(analysis)` **before** saving so a hostile shape is never stored
  (`telegram_bot.py:4003-4004`, `4102-4103`).
- **Display fallbacks** in the meal card (`format_food_result`, `telegram_bot.py:2875-2919`):
  item `estimated_calories` is shown via `safe_number` when numeric but rendered as the raw
  (escaped) string when non-numeric; totals use `analysis.get(...) or "?"` for calories and
  `or 0` for macros. Negative clamps: daily running totals clamp each meal's contribution to
  `max(0, safe_number(...))` (`telegram_bot.py:2927-2932`, `2951-2952`).

---

## 4. NL EXECUTOR (text messages)

Entry point `handle_text_message` (`telegram_bot.py:4240-4356`), hardened 2026-07-16 after a live
crash-loop on a compound correction+delete message (`telegram_bot.py:4359-4367`). Flow:

1. *(server-only fast-path for fitness queries — see §7; optional in app phase 2)*.
2. If quota-paused → refuse with the pause message, no model call (`telegram_bot.py:4253-4261`).
3. Build the meals snapshot (last 7 days of food meals, §1.2) and the prompt; call Gemini in JSON
   mode.
4. Parse errors → user-facing "couldn't understand the AI response, try rephrasing"; transport
   errors → "error contacting AI, try again" (`telegram_bot.py:4308-4315`).
5. Normalize the response into an ordered action list (§4.1); empty →
   "I couldn't work out what to do with that. Try one request at a time, e.g. 'change meal 2 to
   roast duck rice'." (`telegram_bot.py:4325-4333`).
6. Execute each action through the intent table; **per-action failure containment** (§4.9).

### 4.1 `_normalize_nl_actions` (`telegram_bot.py:4184-4237`)

Accepted top-level shapes: a single object, `{"intent":"multi","actions":[...]}`, or a **bare JSON
array** of action objects (produced by real models despite the prompt; crash-looped the bot —
pinned by `test_nl_compound_bare_array_crash_regression`). Rules, in order:

1. If the result is a map: use its `actions` list **only** when it is a non-empty list AND
   (`intent == "multi"` OR the map has no recognized single intent of its own). "Recognized" means
   `intent` is a **string** and one of the five handler intents — the type check is the
   **unhashable-intent guard**: a list/dict-valued `intent` must read as "not recognized", not
   throw from the membership test (`telegram_bot.py:4199-4209`). A real single intent with a
   hallucinated `actions` list wins as a single action
   (`test_nl_single_intent_wins_over_hallucinated_actions`).
2. If the result is a list: use it as the items. Anything else → `[]`.
3. Drop non-map items.
4. Cap at `NL_MAX_ACTIONS` = 5 (`telegram_bot.py:4181`), keeping the first 5
   (`test_nl_compound_caps_at_max_actions`).
5. **Duplicate-delete merging**: if more than one action has `intent == "delete"`, concatenate all
   their list-typed `meal_indices` into the **first** delete action and drop the rest — there is
   only one pending delete-confirmation slot
   (`telegram_bot.py:4224-4236`, `test_nl_compound_duplicate_delete_actions_merge_into_one`).

### 4.2 Dispatch (`telegram_bot.py:4335-4340`)

`intent` → handler map (`telegram_bot.py:4171-4177`): `correction`, `delete`, `new_meal`,
`log_weight`, `log_activity`. Any other value — including non-string intents — falls back to the
**chat** handler (`test_nl_unhashable_or_nonstring_intent_falls_back_to_chat`). All `meal_index`/
`meal_indices` in every action refer to the **same snapshot** the prompt showed; handlers resolve
indices to DB row ids immediately, so earlier actions can never shift a later target
(`telegram_bot.py:4317-4324`, `test_correction_by_index_updates_the_exact_five_day_old_row`).

### 4.3 Index coercion (`_coerce_meal_index`, `telegram_bot.py:3953-3971`)

bool → null; int → itself; float → int only if integral, else null; string → trimmed int parse or
null; anything else null. (`test_nl_correction_hostile_index_never_crashes`,
`test_nl_delete_all_invalid_indices_never_crash_or_stash`.)

### 4.4 `correction` (`_nl_correction`, `telegram_bot.py:3974-4032`)

1. No recent meals → "Cannot correct because no meals are logged recently."
   (`test_b12_delete_intent_with_empty_database` covers the sibling delete case.)
2. Coerce `meal_index` (default key value 0). Null or out of `[0, len)` → error reply showing the
   raw model value **escaped and truncated to 40 chars** — hallucinated markup must neither inject
   formatting nor break the send (`telegram_bot.py:3983-3988`,
   `test_nl_correction_invalid_index_reply_escapes_model_meal_index`). App: render as plain text.
3. **Silent-delete guard**: if the new `analysis` is not a map or `is_food` is falsy, REFUSE and
   leave the meal unchanged — overwriting with a non-food analysis would make every is_food view
   drop the meal, i.e. a delete that skipped the delete confirmation
   (`telegram_bot.py:3990-3999`, `test_nl_correction_refuses_empty_or_nonfood_analysis`). Reply:
   "That correction didn't include a usable updated analysis, so I left the meal unchanged…".
4. Sanitize `food_items` via `safe_food_items` before persisting (`telegram_bot.py:4001-4004`).
5. **Compute the reply diff BEFORE the write**, with `safe_number(…, 0)` on both old and new
   calories — a hallucinated string calorie must not throw *after* the DB row was already
   rewritten (which told the user "failed" for a change that happened)
   (`telegram_bot.py:4006-4016`, `test_correction_reply_escapes_hostile_meal_and_gemini_text`).
6. Update by the snapshot row's DB id (`telegram_bot.py:4018-4021`); reply shows
   `old_desc → new_desc`, `old_cal kcal → new_cal kcal (±diff)` (diff formatted with an explicit
   `+` when positive), plus the model's `reason` if present. All interpolations escaped.

### 4.5 `delete` (`_nl_delete`, `telegram_bot.py:4035-4094` + confirm flow `4412-4451`)

1. **List coercion**: `meal_indices` not a list → accept a bare non-bool int as `[n]`, anything
   else as `[]` — iterating a scalar string like `"12"` must not stage meals 1 and 2
   (`telegram_bot.py:4036-4043`).
2. No meals → "Cannot delete because no meals are logged recently."; empty indices → "Didn't catch
   which meals to delete."
3. Coerce each index (§4.3), de-duplicate, sort ascending, keep only in-range ones; collect
   `(db_id, label)` pairs where label =
   `"{meal_description} ({date} {time}, ~{total_calories or 0} kcal)"`
   (`telegram_bot.py:4054-4068`, `test_nl_delete_mixed_indices_honors_only_the_valid_ones`).
   All invalid → "Couldn't match those meals to the recent list."
4. **Confirmation required — nothing is deleted yet.** Server: an inline-keyboard message listing
   each label, the model's reason, "This cannot be undone.", with Delete/Cancel buttons bound to a
   per-request nonce token; one pending request per chat; TTL
   `NL_DELETE_CONFIRM_TTL_SECONDS` = 600 s (`telegram_bot.py:190`, `4077-4094`). Confirm executes
   `delete_meal` per id (tombstoning ledger rows, §2.4) and replies with the deleted labels;
   cancel discards; a stale/expired/superseded token must neither delete nor discard the newer
   pending set (`telegram_bot.py:4412-4451`). **App**: a modal confirmation dialog listing the
   meal labels with Delete/Cancel. The dialog being modal replaces the nonce/TTL machinery — but
   if the app queues confirmations non-modally it must reproduce the token + 10-minute-expiry
   semantics. (`test_b7_nl_delete_requires_confirmation_then_deletes`,
   `test_nl_delete_cancel_keeps_meals`.)

### 4.6 `new_meal` (`_nl_new_meal`, `telegram_bot.py:4097-4109`)

If `analysis` is a map with truthy `is_food`: sanitize `food_items`, save with
`source='manual_text'`, `image_hash=''` (server passes `file_id="text"`), reply with the standard
meal card (§5.4) prefixed "Added new manual meal". Otherwise: "I couldn't detect food in that
description."

### 4.7 `log_weight` (`_nl_log_weight`, `telegram_bot.py:4112-4128`)

1. First try the deterministic text parser `nutrition.parse_weight_kg` on the **user's raw text**
   (`nutrition.py:433-460`): explicit `kg`/`lb` units (lb × 0.45359237), or a bare number guarded
   by a "weigh…" keyword; rounded to 1 decimal; only accepted within
   [`MIN_WEIGHT_KG`=30, `MAX_WEIGHT_KG`=300] (`nutrition.py:428-430`). The regexes guard against
   scientific-notation and "kilometers" false matches (`nutrition.py:416-427`).
2. Fallback to the model's `weight_kg` field via `safe_number`, same 30–300 bound, rounded to 1
   decimal (`test_nl_log_weight_numeric_field_fallback_saves`,
   `test_nl_log_weight_hostile_field_saves_nothing`).
3. Nothing valid → "I couldn't read a valid body weight (30–300 kg)." and **no write**.
4. Save upsert-by-day for today, `source='telegram'` (app: `'nl'`); reply
   `Logged {kg} kg for {date}.` (`test_nl_log_weight_saves`).

### 4.8 `log_activity` (`_nl_log_activity`, `telegram_bot.py:4131-4158`)

Clamp model fields: `active_calories` → `clamp(safe_number, 0, ACTIVITY_KCAL_MAX=20000)`;
`steps` → round(clamp(safe_number, 0, ACTIVITY_STEPS_MAX=200000)); `distance_km` →
clamp(safe_number, 0, ACTIVITY_KM_MAX=500) (constants `telegram_bot.py:2971-2973`). All three ≤ 0 →
"couldn't find any activity numbers" and no write. Save one `activities` row for today:
`source='manual'`, `activity_type='manual'`, kcal/km stored as null when 0, steps inside
`raw={"steps": n}` (`telegram_bot.py:4142-4147`). Reply lists only the non-zero parts:
`Logged activity: 450 kcal · 8,000 steps · 5 km (2026-07-17).`
(`test_nl_log_activity_saves`, `test_nl_log_activity_keeps_valid_fields_and_drops_junk`,
`test_nl_log_activity_hostile_payload_saves_nothing`.)

### 4.9 `chat` fallback + failure containment

- `chat` (`_nl_chat`, `telegram_bot.py:4161-4168`): show the model's `reply`; a null/non-string/
  blank reply falls back to "I'm not sure what you mean. Try describing a meal or correction!" —
  never render an empty bubble.
- **Per-action containment** (`telegram_bot.py:4335-4356`): each handler runs in its own
  try/catch; one malformed action must not abort its siblings. After the loop:
  - all N failed, N == 1 → "That request failed. Please try again."
  - all N failed, N > 1 → "All {N} requested actions failed. Please try again." — the wording
    must never claim partial success
    (`test_nl_compound_all_failed_wording_never_claims_partial_success`).
  - some failed → "{k} of {N} requested action(s) failed — the rest were applied."
    (`test_nl_compound_partial_failure_continues`).
- The whole executor is additionally wrapped so no exception can crash the app loop
  (`handle_text_message_safe`, `telegram_bot.py:4359-4376`,
  `test_handle_text_message_safe_contains_any_crash`).

### 4.10 Tests to mirror in Dart

From `tests/test_telegram_bot.py` (line numbers are test-def lines): 
`test_b7_nl_delete_requires_confirmation_then_deletes` (577), `test_nl_delete_cancel_keeps_meals`
(629), `test_b12_delete_intent_with_empty_database` (566),
`test_nl_delete_all_invalid_indices_never_crash_or_stash` (715),
`test_nl_delete_mixed_indices_honors_only_the_valid_ones` (740),
`test_nl_correction_hostile_index_never_crashes` (764),
`test_nl_correction_invalid_index_reply_escapes_model_meal_index` (3977),
`test_nl_correction_refuses_empty_or_nonfood_analysis` (2999),
`test_correction_by_index_updates_the_exact_five_day_old_row` (1807),
`test_correction_reply_escapes_hostile_meal_and_gemini_text` (1616),
`test_text_handler_injects_relative_date_context` (794),
`test_nl_compound_bare_array_crash_regression` (2874), `test_nl_compound_multi_object_shape`
(2901), `test_nl_compound_caps_at_max_actions` (2927),
`test_nl_compound_partial_failure_continues` (2940),
`test_nl_compound_all_failed_wording_never_claims_partial_success` (3019),
`test_nl_compound_duplicate_delete_actions_merge_into_one` (3041),
`test_nl_single_intent_wins_over_hallucinated_actions` (3061),
`test_nl_unhashable_or_nonstring_intent_falls_back_to_chat` (1888),
`test_nl_unusable_response_shapes_reply_gracefully` (2962), `test_nl_log_weight_saves` (1253),
`test_nl_log_weight_hostile_field_saves_nothing` (1677),
`test_nl_log_weight_numeric_field_fallback_saves` (1689), `test_nl_log_activity_saves` (1266),
`test_nl_log_activity_hostile_payload_saves_nothing` (1708),
`test_nl_log_activity_keeps_valid_fields_and_drops_junk` (1720),
`test_new_meal_correction_delete_still_route_after_prompt_change` (1321),
`test_handle_text_message_safe_contains_any_crash` (2970).

---

## 5. VIEWS / REPORTS

All server views emit Telegram-HTML; the app renders native widgets with the **same content,
formulas, and number formatting**. Formatting rules used throughout: totals accumulate through
`safe_number`; per-day totals clamp each meal to ≥ 0; estimates are prefixed `~`; thousands
separators on calorie totals; averages/medians rendered as ints (truncation via `int()` for the
history average, `round()` where noted).

### 5.1 Today summary — `/today` (`format_today_summary`, `telegram_bot.py:3841-3894`)

Data: today's **food** meals (`telegram_bot.py:838-842`). Empty → "No meals logged yet today."
Otherwise:

- Totals: `total_cal/p/c/f = Σ safe_number(analysis.total_*)` (no negative clamp here).
- Lines: `🔥 {total_cal:,} kcal`, `Protein: {p}g`, `Carbs: {c}g`, `Fat: {f}g`,
  `Meals: {count}`.
- Activity: `(burned, steps, km) = _activity_totals(today's activities)`
  (`telegram_bot.py:3097-3110`: burned = Σ safe_number(active_calories); steps = Σ
  safe_number(raw.steps) over map-typed `raw`; km = Σ safe_number(distance_km)).
  - `burned > 0` → `Burned: {round(burned):,} kcal` and `Net: {round(total_cal − burned):,} kcal`.
  - else if steps or km → `Activity: {km:g} km · {round(steps):,} steps` (only non-zero parts;
    `:g` = shortest float repr).
  (`test_today_summary_shows_net_when_activity_present`,
  `test_today_summary_omits_net_without_activity`,
  `test_today_summary_shows_distance_only_activity_without_net`,
  `test_today_summary_ignores_activity_from_other_days`.)
- **Typical-day line**: per-day calorie totals over the prior 7 local days *excluding today*
  (window `[today−7, today−1]`, `_daily_calorie_totals` — is_food only, per-meal `max(0,
  safe_number(total_calories))`, grouped by stored `date`, `telegram_bot.py:2942-2953`). Only when
  ≥ 2 days have data: `typical = int(median(day_totals))`;
  show `📊 Typical day: ~{typical:,} kcal` plus either
  `⏳ ~{typical − total:,} kcal headroom vs typical` (when total ≤ typical) or
  `📈 ~{total − typical:,} kcal above typical`. Median, not mean — under-logged days would bias a
  mean low (`telegram_bot.py:3877-3892`).

### 5.2 Today's meal list — `/meals` (`format_meals_list`, `telegram_bot.py:3897-3923`)

Numbered from 1. Per meal: `**{desc}** ({time})[ ✏️ if corrected]` then
`~{cal} kcal | P:{p}g C:{c}g F:{f}g` (each via `safe_number`). Footer:
`🔥 Total: ~{Σcal:,} kcal ({n} meals)`.

### 5.3 History — `/history [days]` (`format_history`, `telegram_bot.py:3926-3950`)

`days` defaults 7, user-clamped to [1, 60] (`telegram_bot.py:5549-5552`); the app's 30-day view is
this with days=30. Window is inclusive: `[today − (days−1), today]`. Empty → "No meals logged in
the past {days} days." Per day (sorted descending): `• {friendly}: ~{kcal} kcal` where friendly is
`Today` for today, else `strftime("%A, %b %d")` (e.g. `Tuesday, Jul 15`). Footer:
`📊 Average: ~{int(sum/len)} kcal / day` — averaged over **days that have data**, not the whole
window.

### 5.4 Single-meal result card (`format_food_result`, `telegram_bot.py:2875-2919`)

Shown after every photo/text meal log. Not-food → "No food detected in this photo." Otherwise:
description header; per item (via `safe_food_items`): `• {name}: ~{cal} kcal` and
`P:{p}g | C:{c}g | F:{f}g` (item cal per §3.5 display fallback); totals
`📊 This meal: ~{total} kcal` (raw `or "?"`) and `P/C/F` (raw `or 0`); the **calorie-mismatch
warning** when `meal_calorie_mismatch` fires: "Item calories sum to ~{item_sum} kcal but the meal
total is ~{total} kcal — reply with a correction if this looks wrong."; then the running daily
totals block (`format_daily_totals`, `telegram_bot.py:2922-2938`:
`Today's Total ({n} meals): / {Σmax(0,cal):,} kcal / P|C|F` with per-meal negative clamps).

### 5.5 Daily report (`daily_report.py:347-494`)

Generated per target date (app: a "Daily report" screen / scheduled local notification).
Structure, in order:

1. **Header**: `📊 Daily Calorie Report`, date as `strftime("%A, %B %d, %Y")`.
2. No food meals → "No meals logged today." (+ fitness sections) and stop.
3. **Meals section** — for each food meal (1-based):
   `{i}. {desc} — {time}[ ✏️]`; every item (via `safe_food_items`, each field `safe_number`):
   `• {name}: {cal} kcal` + `P|C|F`; `📊 Subtotal: ~{cal} kcal | P:{p}g C:{c}g F:{f}g`.
   - **Contradicting-totals flag**: when `meal_calorie_mismatch` fires **and the meal is not
     corrected**: "Item calories sum to ~{item_sum} kcal but the meal total is ~{cal} kcal — this
     entry may be wrong." (`daily_report.py:420-424`).
   - **Likely-duplicate flag** (display-only, no writes): duplicate key = `image_hash` if it is a
     non-empty string, else the tuple `(str(time), str(meal_description), str(total_calories))`;
     a repeated key → "Possible duplicate of meal {first_index}." (`daily_report.py:426-437`).
4. **Daily Summary**: `Total Calories: ~{Σ:,} kcal`, Protein/Carbs/Fat grams, `Meals logged: {n}`
   (sums via `safe_number`, no negative clamp on this path — `daily_report.py:392-399`).
5. **7-day average**: prior window `[date−7, date−1]`; per-day totals counting only food meals
   whose raw `total_calories or 0` is a non-bool number with `0 < cal < 1e9` (note: *strictly
   positive*, and deliberately not `math.isfinite`, which overflows on huge ints —
   `daily_report.py:450-470`). Only when ≥ 2 prior days have data:
   `📈 7-day avg: ~{round(mean):,} kcal` and `Today vs avg: {delta:+,} kcal ({delta/avg*100:+.0f}%)`
   (percentage only when avg ≠ 0).
6. **Macro Split**: kcal-weighted percentages `P×4 / C×4 / F×9` of `total_macro_cal`, each
   `round()`ed, only when `total_macro_cal > 0` (`daily_report.py:481-490`).
7. **Fitness sections** (`_fitness_sections`, `daily_report.py:273-344`) — each individually
   guarded; the entire block is omitted when the user has no fitness footprint (no profile,
   weigh-in, activity, or workout):
   - *Diet Targets* (only with a configured `diet_mode`): `nutrition.profile_diet_targets` over
     the **anchor weight** (latest weigh-in at/before the report date, looking back up to 366
     days — `daily_report.py:51`, `294-303`), then `nutrition.analyze_macros` on the day's meals
     and `nutrition.format_macro_report` (§7.2).
   - *Energy Balance* (only when today has burn, steps, or distance): active burn, steps,
     distance; `⚖️ Net: ~{consumed − burn:,} kcal (consumed − active burn)` — net subtracts
     **active** burn by default; a `GARMIN_NET_USE_TOTAL` knob switches to whole-day total
     (`daily_report.py:118-124`, `189-231`). App default: active basis.
   - *Weight* (only with weigh-ins in the trailing `WEIGHT_TREND_WINDOW_DAYS` = 7 window,
     `daily_report.py:49`): `Latest: {kg:.1f} kg` and the least-squares trend
     `7-day trend: {⬆️/⬇️/➡️} {slope:+.2f} kg/wk` (slope = per-day OLS fit on (ordinal day,
     kg > 0 via safe_number) × 7, ≥ 2 points required — `daily_report.py:142-171`).
   - *Today's Training*: `fitness_plan.todays_workout(...)` line; plus the **weeks-to-race line**
     when `goal_race_date` is set and `fitness_plan.weeks_to_race` returns non-null:
     `🎯 {weeks:.1f} weeks to race ({goal_race_date[:10]})` (`daily_report.py:251-270`).

### 5.6 Stats — `/stats` (`format_database_stats`, `telegram_bot.py:2748-2778`; aggregates `database.py:453-564`)

All-time aggregates (window `1970-01-01 .. today`), computed in SQL with the exact truthiness/
clamp semantics of §3.5 (`_STATS_FOOD_SQL` `database.py:453-461`, `_STATS_CALORIES_SQL`
`467-472`), with a row-scan fallback (`database.py:486-503`). Output lines:

```
📈 Database Stats
Food meals today: {n}
Food meals last 7 days: {n}          # window [today−6, today]
Food meals all time: {food_meals}
Raw DB rows all time: {total_meals}   # includes not-food rows
Calories last 7 days: ~{int(Σ safe_number):,}
Calories all time: ~{int(total):,}
Average per active day: ~{int(total_calories / active_days):,} kcal   # only when active_days > 0
Sources                                # only when any; sorted by count desc then name
• {source}: {count}                    # ''/null source → 'unknown'
```

`active_days` = distinct non-empty `date` over food meals (`database.py:515-516`).

### 5.7 Recent meals — `/recent` (`format_recent_meals`, `telegram_bot.py:2781-2801`)

Last 3 days of food meals, last `limit` (default 10, clamp 1–25) entries, showing each meal's
**0-based snapshot-style index** `[i]`, description, date, time, ✏️ marker, `~{cal} kcal` (raw
`or 0`). Footer: "These indexes match natural-language correction/delete context." Useful for the
app's meal-edit affordance.

---

## 6. PHOTO INTAKE (native re-implementation of the Android watcher)

The server pairing is: a Termux watcher uploads camera photos to Flask `/upload`, plus a nightly
`--sync` reconcile (`android/upload_photo.py`). The app replaces both with a native new-photo
listener + periodic backfill scan over the photo library, feeding the same pipeline (§2.3 strict
reservation → §3 analysis → save/skip/fail).

### 6.1 New-photo detection & supported types

Watch the camera roll for new images (server analog: watcher on `DCIM/Camera`,
`upload_photo.py:77-81`). Extensions accepted by the queue/watcher:
`{".jpg", ".jpeg", ".png", ".heic", ".heif"}` (`SUPPORTED_QUEUE_EXTENSIONS`,
`upload_photo.py:69`); the server additionally accepts `.tiff` (`SUPPORTED_EXTENSIONS`,
`config.py:44`) and sniffs magic bytes for JPEG/PNG/HEIC-ISO-BMFF/TIFF/WebP
(`telegram_bot.py:1825-1835`). App rule: accept jpg/jpeg/png/heic/heif/tiff; validate content by
decodability, not extension.

### 6.2 Dedup = md5 of ORIGINAL bytes

The identity of a photo is `md5(original file bytes)` (`upload_photo.py:256-262`, declared as
`original_hash` even when the uploaded bytes are recompressed — `upload_photo.py:436-457`,
honored server-side at `telegram_bot.py:4865-4872`). The app hashes the **original library asset
bytes** (not the normalized JPEG) and keys the `photo_ingestions` ledger and `meals.image_hash` on
it. This is what makes re-scans idempotent.

### 6.3 captured_at derivation

Priority order:

1. **Filename timestamp** (`_captured_at_from_filename`, `upload_photo.py:406-423`): regex
   `(?:^|[^0-9])(\d{8})_(\d{6})` over the filename — e.g. `IMG_20260716_193042.jpg` → parse
   `%Y%m%d%H%M%S` as device-local wall time; invalid datetime → none.
2. *(app improvement, allowed)*: the photo library's own creation date (EXIF/asset metadata) —
   the native API gives what the filename hack approximates. Treat it as the same field.
3. **Fallback**: no valid capture time → date the meal at intake time
   (`upload_photo.py:412-414`; server fallback `telegram_bot.py:1812`).

**Validation** (`_parse_captured_at`, `telegram_bot.py:1838-1853`): format
`YYYY-MM-DD HH:MM:SS`; reject if more than **1 hour in the future** of local now, or older than
`CAPTURED_AT_MAX_AGE_DAYS` = **45 days** (env-clampable 1–365, `telegram_bot.py:1822`). A
rejected value falls back to intake-time dating rather than trusting junk. A valid `captured_at`
sets the meal's `date`/`time` (§2.2).

### 6.4 Backfill / reconcile (replaces `--sync`)

Server semantics to reproduce (`sync_photos`, `upload_photo.py:518-581`): periodically (nightly)
scan the camera roll for photos whose **mtime day** falls within the last `SYNC_LOOKBACK_DAYS`
(default **2**, meaning today + yesterday; range 1–30, `upload_photo.py:68`); md5 each; ask the
ledger which hashes are unknown; process only the missing ones. "Missing" excludes: hashes with a
meal row, hashes reserved/tracked in any ledger status (incl. `deleted`/`skipped` tombstones —
`database.py:376-399`, `telegram_bot.py:1903-1926`), and (server-only) staged/failed files.
**Every photo in the window is offered to the AI for classification** — keep the window small
(`upload_photo.py:526-532`). Non-food photos get ledger status `skipped` so they are never
re-analyzed (`telegram_bot.py:5046-5048`).

### 6.5 Offline behavior

Server analog: failed uploads are copied into an offline queue named `<md5-12>__<original-name>`
(dedup by content, atomic `.part`-then-rename staging — `upload_photo.py:264-304`), drained
oldest-first up to 10 per heartbeat, pausing on the first failure (`upload_photo.py:321-354`);
permanently-rejected files are quarantined instead of retried forever (`upload_photo.py:74`,
`306-319`). **App equivalent**: with no server there is only "Gemini unreachable". A photo that
fails analysis for network/transient reasons keeps ledger status `failed` (original path
retained); a "retry failed" action — and optionally an on-reconnect trigger — re-runs analysis
with `reclaim_statuses={"failed"}` (§2.3). During a quota pause, intake stages photos as `failed`
without calling Gemini (§3.3). Nothing is lost while offline because the ledger + camera roll are
both local.

---

## 7. FITNESS (phase-2 scope — spec only, app ships later)

### 7.1 Logging fields + validation clamps

- **Weight**: `body_weight` row per §2.1; one per local day, re-log overwrites
  (`database.py:744-762`). Accept only [30, 300] kg (`nutrition.MIN_WEIGHT_KG`/`MAX_WEIGHT_KG`,
  `nutrition.py:429-430`); parse text via `parse_weight_kg` (§4.7). Staleness notice threshold:
  `WEIGHT_STALE_AFTER_DAYS` = 14 (`telegram_bot.py:2976`); trend window
  `WEIGHT_TREND_WINDOW_DAYS` = 7 (`telegram_bot.py:2980`, `daily_report.py:49`).
- **Activity**: clamps `ACTIVITY_KCAL_MAX` = 20000, `ACTIVITY_STEPS_MAX` = 200000,
  `ACTIVITY_KM_MAX` = 500, all floored at 0 (`telegram_bot.py:2971-2973`, applied §4.8).
- **Workouts**: free-form strength log — `workout_type` default `strength`, `muscle_groups` from
  `FITNESS_MUSCLE_GROUPS` = (legs, chest, shoulders, back, arms, core)
  (`telegram_bot.py:2964`), optional duration/notes. Training-recommendation lookback:
  `TRAIN_LOOKBACK_DAYS` = 14 (`telegram_bot.py:2978`).

### 7.2 Diet modes / macro targets (`nutrition.py`)

- Modes: `keto`, `high_protein`, `balanced` (`nutrition.py:17`); unknown mode → `balanced`
  (`nutrition.py:59-61`). Specs (`nutrition.py:30-49`):

| mode | protein g/kg | carb cap | %kcal split P/C/F |
|---|---|---|---|
| keto | 1.6 | 50 g hard cap | 23 / 5 / 72 |
| high_protein | 2.0 | — | 30 / 40 / 30 |
| balanced | 1.6 | — | 30 / 40 / 30 |

- `diet_targets(mode, weight_kg, calorie_target, protein_g_per_kg)` (`nutrition.py:89-140`):
  protein anchored to body weight (`round(g_per_kg × kg)`) when known; calorie targets clamped up
  to `MIN_CALORIE_FLOOR` = 1200 (`nutrition.py:24`); per-macro grams derived from the %kcal split
  with Atwater factors 4/4/9 (`nutrition.py:20`); keto caps carbs at 50 g; split-derived protein
  only when weight is unknown.
- `profile_diet_targets` (`nutrition.py:143-159`): explicit stored gram targets from the profile
  override mode-derived ones.
- `analyze_macros(meals, targets)` (`nutrition.py:200-340`): consumed = per-analysis clamped-≥0
  sums; status bands per macro: ok within ±`max(5 g, 10 %)` of target, else short/over
  (`nutrition.py:177-186`); keto carbs use the hard cap (over only above cap); concrete non-medical
  suggestions (protein food ladder `nutrition.py:189-197`); calories get the same banding.
- Rendering: `format_macro_report` multi-line block always ending with the disclaimer
  "Informational only — not medical advice." (`nutrition.py:26`, `343-384`); one-line summary
  `report_line` (`nutrition.py:387-413`).

### 7.3 VDOT / running features (name-only; the app ships these later)

Module `fitness_plan.py` (pure): `vdot_from_race(distance_m, seconds)` (`fitness_plan.py:83`),
`paces_from_vdot(vdot)` (`:145`), `format_pace` / `format_pace_mi` / `per_mile` (`:172-198`),
`default_profile(chat_id, today)` (`:224`), `phase_for_date(profile, day)` (`:245`),
`weeks_to_race(profile, day)` (`:272`), `todays_workout(profile, day)` (`:328`),
`week_plan(profile, day)` (`:353`). Constants: `DEFAULT_VDOT` = 45, clamp
[`VDOT_MIN` 20.0, `VDOT_MAX` 85.0], `DEFAULT_LONG_RUN_DAY` = 6 (Sunday), pace %VO2max bands
(`fitness_plan.py:20-46`). Command surface on the server: `/run`, `/train run vdot|race`, `/plan`,
`/macros`, `/diet`, `/weight`, `/activity`, `/workout`, `/train`
(`telegram_bot.py:3118-3810`), plus a deterministic no-model fast-path for read-only run/macro/
plan questions (`_maybe_answer_fitness_query`, `telegram_bot.py:3812-3839`). Phase-2.

---

## 8. SETTINGS / KNOBS (app equivalents of the env knobs)

| Setting | Default | Source | App relevance |
|---|---|---|---|
| Gemini model | `gemini-2.5-flash` | `config.py:26` | user-editable model string |
| Gemini API key | — | `config.py:25` | user-supplied, stored in secure storage |
| Analysis max attempts (auto intake) | 3 (clamp 1–10) | `telegram_bot.py:122` | §3.3 |
| Retry base delay | 5 s | `telegram_bot.py:123` | §3.3 |
| Retry max delay | 60 s | `telegram_bot.py:124` | §3.3 |
| Daily-quota pause cooldown | 12 h (clamp 60 s–7 d) | `telegram_bot.py:125` | §3.3 |
| Gemini HTTP deadline | 90 s (clamp 15–600) | `telegram_bot.py:154` | §3.2 |
| Photo long-side / JPEG quality | 1568 px / q85 | `telegram_bot.py:1151-1157` | §3.1 (not user-editable) |
| NL edit window (`TEXT_EDIT_WINDOW_DAYS`) | 7 d (clamp 1–31) | `telegram_bot.py:161` | §1.2 snapshot |
| NL max actions | 5 | `telegram_bot.py:4181` | §4.1 (constant) |
| Delete-confirm TTL | 600 s | `telegram_bot.py:190` | §4.5 (only for non-modal UIs) |
| Duplicate window | 5 min | `config.py:95` | §2.3 |
| Stale `processing` reclaim | 6 h | `database.py:11` | app: reclaim on launch (§2.3) |
| `CAPTURED_AT_MAX_AGE_DAYS` | 45 (clamp 1–365) | `telegram_bot.py:1822` | §6.3 |
| `SYNC_LOOKBACK_DAYS` (backfill window) | 2 (clamp 1–30) | `upload_photo.py:68` | §6.4; user-widenable after outages |
| Max photo size | 25 MB | `telegram_bot.py:139` | refuse larger inputs before decode |
| Max concurrent analyses | 3 (clamp 1–16) | `telegram_bot.py:145` | app: serialize or small pool for backfill |
| `ECHO_UPLOAD_PHOTOS` | on | `telegram_bot.py:136` | **N/A** — the app UI always shows the photo with its card |
| History days clamp | default 7, 1–60 | `telegram_bot.py:5550` | §5.3 |
| Dietary profile text | empty | `config.py:84-91` | §1.3, settings text field |
| Net-burn basis (`GARMIN_NET_USE_TOTAL`) | active | `daily_report.py:118-124` | §5.5, phase-2 |
| Boolean settings parsing | tri-state | `utils.py:100-110` | `parse_boolish` semantics |

Non-ported server knobs (no app meaning): Telegram token/chat id, PushPlus/WeChat, VPN
enforcement (`telegram_bot.py:162-189`), watchdog/systemd (`telegram_bot.py:5136+`), Flask API
key, heartbeat staleness warnings.

## §9 App-only divergences (2026-07-24 hardening)

Deliberate departures from server behavior, driven by mobile realities the
server never faced (EMUI process kills, free-tier quotas, two isolates on
one SQLite file). Each is pinned by tests named alongside.

| Divergence | Server behavior | App behavior | Why | Pinned by |
|---|---|---|---|---|
| Transient analysis failure | mark `failed` | RELEASE the reservation (`AnalysisOutcome.retryable`) | automated intake never reclaims `failed`; a 12 h quota latch would silently drop photos forever | `photo_pipeline_test` retryable-release |
| Launch reclaim sweep | all `processing` → `failed` | `app_watch` rows RELEASED, only >15 min stale rows touched | EMUI kills mid-analysis routinely; a live background isolate may own fresh rows | `meals_dao_reclaim_test` |
| Backfill cadence | nightly `--sync` | 30-min WorkManager + launch/resume full-window catch-up + `safeFrontier` watermark | the app cannot rely on cron; watermark keeps 48 scans/day cheap | `background_glue_test` watermark suite |
| Photo intake concurrency | sequential bot loop | backpressured sink (`PhotoIntake.attachSink`), one photo's bytes resident | 200-photo backlog ≈ 1 GB resident otherwise (OOM) | `watcher_test` backpressure |
| Key validation | n/a (server key assumed valid) | quota-class HTTP responses count as ACCEPTED | a 429 proves auth; refusing to save locked onboarding for hours | `gemini_analyzer_test` validateKey 429 |
| Share intake files | n/a | plugin container copies deleted after consumption | receive_sharing_intent contract; unbounded growth otherwise | `share_intake_test` cleanup |
| Decode ceiling | none (server CLI) | 50 MP header-probe cap before full decode | 108 MP JPEG ≈ 432 MB RGBA in a background isolate | normalize cap (visual) |

### §9.0 divergences added 2026-07-25..27 (the table above was frozen at 07-24)

| Divergence | Server behavior | App behavior | Why | Pinned by |
|---|---|---|---|---|
| Editable history | corrections only via NL | full editor: description, date/time move, per-item rows, delete | a mis-typed number should not need a sentence to the model | `meal_edit_logic_test`, `meal_editor_screen_test` |
| Meal thumbnails | none (Telegram holds the photo) | 160 px JPEG per photo meal in `meal_thumbs`, lazily backfilled from the library | the numbers are only checkable against the picture; gallery originals get deleted | `meal_thumbs_test`, DAO cascade test |
| Photo-coverage audit | n/a | manual md5 sweep vs the ledger + one-tap log/retry/re-analyze | the watcher can be frozen by the OS for a whole day; the user needs a way to ask "did you get everything?" | `coverage_test`, `coverage_screen_test` |
| Describe-a-meal | any NL intent in one channel | dedicated screen forcing new-meal semantics (empty meals list) with an editor PREVIEW before insert | a model guess about food it never saw must not enter totals unreviewed | `describe_meal_test`, `add_text_screen_test` |
| Analysis provider | Claude CLI → Gemini fallback | SEVEN selectable providers: Gemini, OpenAI, Anthropic, own-server → subscription, plus mainland-China Qwen/Doubao/GLM (2026-07-29) | zero API cost via the subscription; the China trio because Gemini/OpenAI/Anthropic are unreachable there without a VPN — for those users a domestic provider is the only way the app works | `provider_analyzers` suites, `server_analyzer_test` |
| EXIF meal dating (2026-07-31) | §6.3 dating = filename timestamp → asset createDate → intake time | the app adds ONE more source before the intake-time fallback: the JPEG's own EXIF DateTimeOriginal (else IFD0 DateTime), read via package:image `decodeJpgExif`, under the same §6.3 validation window. §6.3 priority is otherwise unchanged | share-sheet photos have no library asset; with no filename timestamp (WeChat saves, downloads) meals were silently dated at intake time — the camera's shutter stamp is the only remaining truth | `test/photo/exif_dating_test.dart` |
| Server backend (serverBackend, 2026-07-30) | server always analyzes on the Claude subscription | app-only knob `serverBackend` ∈ claude/glm/doubao, sent as `backend` with /api/analyze_photo and /api/text_intent; the server runs the SAME Claude Code CLI against the chosen plan (GLM Coding Plan via `GLM_PLAN_KEY` + official zai vision MCP; Doubao Agent Plan via `DOUBAO_PLAN_KEY`); /api/auth_check reports per-backend readiness; replies carry `analyzed_by` and the app REFUSES a mismatch (a pre-upgrade server would silently bill the Claude plan) | both vendors officially support Claude Code under their plans (verified 2026-07-30); vendor runs scrub Anthropic credentials from the child env | `server_analyzer_test`, `test_claude_analyzer.py`, `test_api_analyze_endpoints.py` |
| Textual meal evidence | photo content only | order screenshots / receipts / nutrition labels count as meals; menus, ads, recipes, feeds, cancelled orders do not | a tracker should log what was EATEN, and a phantom meal is worse than a missing one | `test_shared_sync.py` (both rule sets) |
| In-app refresh signal | n/a (Telegram pushes) | `mealsChangedSignal` — any save re-queries open screens | a meal saved while a screen was open stayed invisible (live report 07-26) | `tab_refresh_test` |

### §9.1 `source` vocabulary (app)

FIVE wire values, defined once in `core/contracts.dart` as `MealSource` and
never spelled as literals at call sites — the reclaim sweep MATCHES on
`app_watch` in SQL, so a drifting literal silently changes intake policy
instead of failing.

| Value | Written by | Reservation policy |
|---|---|---|
| `app_photo` | in-app picker, share sheet | deliberate — reclaims failed/skipped/deleted |
| `app_watch` | watcher, background job, catch-up scans | strict — never reclaims; the ONLY value the launch sweep releases |
| `manual_text` | describe-a-meal screen, NL `new_meal` | n/a (no photo); server parity, `telegram_bot` writes the same string |
| `app_manual` | meal editor, numbers typed by hand | n/a; distinct from `manual_text` to record that NO model produced the values |
| `app_manual_photo` | meal editor reached from the coverage screen's "log it yourself" | a photo EXISTS and its md5 is ledgered, but every number came from the user |

Pinned by `test/core/meal_source_test.dart` (frozen strings + distinctness).
