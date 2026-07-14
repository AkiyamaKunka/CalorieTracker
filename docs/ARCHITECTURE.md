# CalorieTracker — Architecture & Ops Runbook

A reference for how the system fits together, why the tricky parts work the
way they do, and how to operate it. Distilled from the code; verify against
the source before relying on any specific number.

## 1. System overview

CalorieTracker is a single-user pipeline that turns food photos into logged
meals and a nightly report. Three ingress paths feed one analysis-and-storage
core.

```
 Android (Termux watcher) ─┐
   upload_photo.py + sh    │  multipart POST /upload  (X-API-Key)
                           │
 iOS (Shortcut)  ──────────┼──►  Flask upload API  ──►  photo-hash
   camera-close upload     │     (telegram_bot.py,      reservation ledger
                           │      port 5000)            (photo_ingestions)
 Telegram (direct photo) ──┘            │                     │
                                        ▼                     ▼
                                  Gemini 2.5 Flash  ──►  SQLite (meals.db)
                                  (vision + intents)         │
                                        ▲                     ▼
 Telegram (text: correct/     ────────┘             daily_report.py (23:30)
   delete/log/chat)                                  ──► Telegram + PushPlus

 meal_relay.py (localhost:8765) — legacy path: accepts pre-analyzed meal
   JSON and writes straight to meals.db (bypasses Gemini).
```

- **Entry point:** `python3 telegram_bot.py` runs two loops — a Flask API on
  `0.0.0.0:5000` (daemon thread) and the main-thread Telegram long-poll.
- **Analysis:** Google Gemini (`GEMINI_MODEL`, default `gemini-2.5-flash`),
  requested in native-JSON mode; images are downscaled before the call.
- **Storage:** one SQLite file, `~/CalorieTracker/meals.db` (WAL). Seven
  tables: `meals`, `heartbeats`, `photo_ingestions`, `body_weight`,
  `workouts`, `activities`, `fitness_profile`.
- **Reporting:** `daily_report.py` is a pure formatter (no LLM) fired by
  launchd/cron at ~23:30 local. Before formatting it performs an optional,
  self-guarded Garmin pull (`_sync_garmin_activity`, backed by `garmin.py`);
  `generate_report` itself stays offline.
- **Fitness:** `nutrition.py` (diet-mode/macro math), `fitness_plan.py`
  (Daniels VDOT run planning), and `garmin.py` (config-gated activity pull)
  feed the report's `_fitness_sections`.

## 2. The ledger / dedup model

Every photo, regardless of source, is deduplicated through the
`photo_ingestions` table before it can become a meal. A row is keyed by
`(chat_id, image_hash)` and moves through these states:

| Status | Meaning | Set by |
| --- | --- | --- |
| `processing` | Reserved; analysis in flight | `reserve_photo_hash` |
| `saved` | Analyzed and logged as a meal (`meal_id` set) | `mark_photo_hash_status` |
| `skipped` | Gemini said "not food" | `mark_photo_hash_status` |
| `failed` | Analysis failed; file kept for `/retry_failed` | failure paths |
| `deleted` | Tombstone — meal deleted, or failed upload cleared | `delete_meal`, `discard_failed_photo_hashes_by_prefix` |

Key rules that make dedup correct:

- **Atomic reservation.** `reserve_photo_hash` runs a `BEGIN IMMEDIATE`
  transaction: it rejects if a meal already exists for the hash, reclaims a
  stale `processing` row (older than `PHOTO_RESERVATION_STALE_SECONDS`, 6h) or
  a caller-listed reclaimable status, else inserts a fresh `processing` row.
  Concurrent reservers of the same hash yield exactly one winner.
- **Original-hash contract.** The phone may recompress a photo before upload,
  so the client declares the *original* DCIM file's md5 in an `original_hash`
  form field. That declared hash — not the recompressed bytes — becomes the
  ledger/meals key, so the nightly `--sync`/`/reconcile` (which hash originals)
  still dedup correctly. Malformed values fall back to hashing the bytes.
- **`/reconcile` suppression.** Android sends its recent photo hashes;
  `_reconcile_missing_hashes` returns only those the server has never seen —
  logged, reserved (non-stale), in-flight, or failed-and-kept hashes are all
  suppressed, so recovery never double-logs. Tombstones (`deleted`) stay
  suppressed too, so a deleted meal is not auto-resurrected.
- **Cross-source dedup reality.** The same physical meal sent via *both*
  Telegram and Android is not deduplicated by bytes — Telegram delivers its
  own recompressed rendition whose md5 can never equal the DCIM original. This
  is inherent, not a bug; in normal use each photo takes one path.

## 3. Failure-recovery design

The system assumes crashes, dead networks, and quota exhaustion happen, and
degrades without losing data.

- **Boot sweep** (`_sweep_stranded_pending_uploads`): on startup, any file
  still in the pending dir (a crash mid-analysis) is moved to the failed store
  and marked `failed` under its declared hash, recoverable via
  `/retry_failed`. Orphaned `processing` reservations with no backing file are
  released so the photo can be re-sent.
- **SIGTERM handling:** a handler turns SIGTERM into the normal
  KeyboardInterrupt shutdown so in-flight non-daemon upload threads finish
  instead of being severed.
- **Offline queue (phone):** when the server is unreachable, `upload_photo.py`
  queues the photo in `~/.offline_queue` and drains it after the next
  successful heartbeat. Permanently-rejected photos (app-origin 400/413/415)
  are quarantined in `~/.offline_queue/rejected/` instead of wedging the
  queue.
- **Gemini quota circuit breaker:** a daily free-tier quota error pauses all
  analysis for `GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS` (12h), persisted to
  `service_health.json`; quota-paused photos are kept with keep/discard
  buttons rather than dropped.
- **Stale-heartbeat warning:** if the phone stops pinging for
  `HEARTBEAT_STALE_WARNING_HOURS` (2h), the bot warns you in Telegram — so a
  dead watcher surfaces instead of photos silently piling up. The cooldown
  re-arms on recovery and retries a failed send in ~15 min.
- **Daily-report catch-up:** auto mode targets the correct local date (the
  previous day on an early catch-up run) and dedupes via the health ledger, so
  a machine asleep at report time delivers the missed report on its next run.
- **Cross-process health writes:** `service_health.py` `update()` wraps every
  read-modify-write of `service_health.json` in an `flock`, so the bot and the
  cron report process cannot lose each other's records.

## 4. Configuration

Secrets and tunables live in `.env` (see [`.env.example`](../.env.example) for
the full list and defaults). The most operationally relevant:

| Var | Purpose |
| --- | --- |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Analysis model |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Bot + single-user allowlist |
| `ANDROID_API_KEY` | Upload API key (phones must match) |
| `MEAL_RELAY_HOST`, `MEAL_RELAY_API_KEY` | Legacy relay bind/key |
| `HEARTBEAT_STALE_WARNING_HOURS` | Phone-offline warning threshold (2h; 0 off) |
| `TRUSTED_PROXY_ENABLED` | Honor `X-Forwarded-For` (default off) |
| `MAX_API_UPLOAD_BYTES` | Upload size cap (25 MB) |
| `GARMIN_ENABLED` | Turn on the daily Garmin activity pull (default off) |
| `GARMIN_TOKEN_DIR` | Pre-minted token directory — token-only auth, minted off-server |
| `GARMIN_NET_USE_TOTAL` | Net calories against Garmin's whole-day total instead of active-only |

Phone-side knobs (`PING_INTERVAL_SECONDS`, `SEED_FRESH_MINUTES`,
`CALORIE_RECOMPRESS_MAX_EDGE`, `CAMERA_DIR`, …) are documented in the README's
Android section.

## 5. Ops runbook

**Deploy the server (GCP VM).**
1. `git pull` the branch on the VM.
2. Restart the service: `sudo systemctl restart caloriebot.service`
   (unit name overridable via `CALORIE_BOT_SERVICE_NAME`).
3. Confirm: `sudo systemctl status caloriebot.service`, then `/status` in
   Telegram.

**Update the phone (Android).**
1. `adb push android/upload_photo.py android/android_watcher.sh android/install_and_start.sh /sdcard/Download/`
   and `adb push android/shortcuts /sdcard/Download/shortcuts`.
2. Tap the 🔁 update widget (or `bash /sdcard/Download/install_and_start.sh`
   in Termux). The installer syntax-checks the payload before stopping the
   running watcher, so a truncated push cannot leave the phone watcher-less.
3. Optional: `pip install pillow` in Termux to enable pre-upload recompression.

**Rotate the Telegram bot token.**
1. `/revoke` then `/token` with @BotFather to mint a new token.
2. Update `TELEGRAM_BOT_TOKEN` in the VM's `.env` and restart the service.
3. The old token exists in early git history — rotation is required before any
   public publish (see [SECURITY.md](../SECURITY.md)).

**Check health.**
- `/status` — heartbeat age, Gemini state, upload queue depth, last report.
- `/doctor`, `/gemini`, `/vpn`, `/report_status` — targeted diagnostics.
- `logs/service_health.json` — the raw ledger (Gemini events, quota pause,
  VPN evidence, daily-report outcomes).

**Common failures.**

| Symptom | Likely cause / fix |
| --- | --- |
| No daily report | Machine asleep at 23:30 (catches up next run), or `TELEGRAM_CHAT_ID` non-numeric. Check `/report_status`. |
| Photos not arriving | Watcher dead or phone offline — the bot warns after 2h; check `~/watcher.log` and `/android`. |
| "Gemini paused" replies | Daily free-tier quota hit; 12h circuit breaker. Kept photos recover via `/retry_failed` after it clears. |
| Uploads rejected on cellular | Carrier blocks port 5000 — clients try port 80 first (needs the VM's 80→5000 redirect, not in the repo). |
| Duplicate meals | Should not happen via one path; if seen, check `photo_ingestions` for a missing/`deleted` row and the reconcile suppression.
