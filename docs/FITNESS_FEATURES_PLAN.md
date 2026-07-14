# Fitness Features — Build Plan

Roadmap for the six health/fitness features requested 2026-07-11. Everything
is additive (new tables, new modules, new commands) — no change to existing
meal/upload/report behavior. Built incrementally; each step is independently
testable.

## New data (all `CREATE TABLE IF NOT EXISTS` in `init_db`)

- **body_weight** — one canonical weigh-in per user-local day (`UNIQUE(chat_id,date)` upsert). `weight_kg`, `source`, `note`.
- **workouts** — strength/lifting sessions, append-only. `muscle_groups`, `workout_type`, `duration_min`, `notes`.
- **activities** — per-day cardio aggregate, `UNIQUE(chat_id,source,external_id)` upsert. `active_calories`, `distance_km`, `duration_min`, `avg_hr_bpm`, source = garmin|healthkit|manual|manual_text.
- **fitness_profile** — one row/user: diet mode + macro targets + body goals + running/VDOT + goal race. Partial upsert (whitelist columns, bound values).

Dates are user-local `YYYY-MM-DD` (`database.user_local_now().date()`); `logged_at`/`updated_at` are server-clock ISO.

## New modules

- **nutrition.py** (pure) — diet-mode targets (keto / high-protein / balanced), macro analysis vs target, weight parsing. No DB/network/Gemini imports.
- **fitness_plan.py** (pure) — Daniels VDOT from a recent race, the five paces (E/M/T/I/R), phase-based marathon schedule, today's workout.
- **garmin.py** (import-guarded, config-gated) — pulls daily active calories; degrades to nothing when unconfigured or the optional `garminconnect` dep is absent.

## Telegram surface (additive)

Commands: `/weight`, `/diet`, `/macros`, `/workout`, `/activity`, `/train_run`, `/plan`, `/profile`. New NL write-intents kept conservative — only `log_weight` and `log_activity` added to the Gemini prompt (macros/today's-run stay deterministic regex, zero Gemini spend, zero meal-classifier regression risk). New `POST /activity` Flask route (X-API-Key) for an iOS Health-export Shortcut.

## Report additions (guarded, omitted when data absent → byte-identical when empty)

Diet Targets · Energy Balance (net = consumed − active burn) · Weight trend · Today's Training. All flow through `_html_to_plain` into the saved .md and WeChat mirror.

## Build sequence

| # | Step | Needs Garmin |
|---|------|:---:|
| 0 | ✅ Feature 1: relative-date NL meal edits (`1476da7`) | — |
| 1 | DB foundation: 4 tables + accessors | no |
| 2 | `nutrition.py` + tests | no |
| 3 | `fitness_plan.py` (Daniels) + tests | no |
| 4 | Diet/weight commands + `log_weight` intent | no |
| 5 | Running commands (`/plan`, `/train_run`, `/workout`) | no |
| 6 | Report integration (`_fitness_sections`) | no |
| 7 | Manual + HealthKit activity (`/activity`, POST route, net line) | no |
| 8 | `garmin.py` live pull + `/status` sync line | **yes** |

## Decisions taken (adjustable later)

- Default diet mode: **balanced** (report unchanged until you run `/diet`).
- Net calories: **consumed − active** (excludes BMR; right for deficit/marathon tracking). `GARMIN_NET_USE_TOTAL=1` opts into TDEE-style.
- Body weight: one weigh-in/day (re-logging overwrites).
- Daniels paces: reproduce published tables within a few sec/mi for VDOT ~40–58; documented note for higher VDOT.

## Needs your input (Step 8 only)

- **Garmin connection**: OK to store auto-refreshing Garmin session tokens (minted once off-server, password never on the VM) under a gitignored dir on the GCP VM? Does your account have MFA? If not, we ship the manual `/activity` + iOS-Shortcut→`/activity` paths and skip the server-side auto pull.
- Goal race date + a recent race time (for VDOT) whenever you have them — until then a sensible default VDOT is used.
