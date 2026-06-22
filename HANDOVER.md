# CalorieTracker Agent Handover Guide

Welcome! If you are an agent taking over this project, here is exactly where we stand and how the project is structured.

## 1. Project Context
The user (Robert) has built a CalorieTracker bot. Originally, this relied on complex local macOS/Android background scripts (ntfy, iCloud syncing) to scan directories and process photos locally. 

**Recent Change:** We have completely migrated the architecture to be **Cloud-Ready** and **Telegram-Native**.
- We deleted the legacy sync scripts (`photo_scanner.py`, `ntfy_sync.py`, `android/calorie_watcher.py`).
- The bot now receives photos directly via Telegram (`bot.get_file`).
- We replaced the fragile `telegram_meals.json` file with a proper **SQLite database** (`meals.db`) via `database.py`.

## 2. File Locations
The project root is `/Users/robertwong/CalorieTracker/`.
- `telegram_bot.py`: The main long-polling bot that handles messages, photos, and natural language corrections using the Gemini 2.5 Flash model.
- `database.py`: SQLite wrapper for `meals.db`. Handles multi-user (`chat_id`) meal insertion, querying, and updating.
- `daily_report.py`: Generates the daily markdown summary and sends it to Telegram and WeChat (via PushPlus). Intended to be run via a cron job at 11:30 PM.
- `config.py`: Environment variables, tokens, and paths.
- `requirements.txt`: Python dependencies (`google-genai`, `Pillow`, `requests`).

## 3. Git Status
Git is fully initialized! The current state has been committed to the `master` branch. You can use standard `git log` and `git diff` commands to track your future changes.

## 4. Current State
The user is currently at a cafe and preparing to migrate this codebase to a brand new IDE, and then eventually deploy it to a free Google Cloud Platform (GCP) e2-micro instance. 

**IMPORTANT:** All local background processes on this Mac (`launchd` plists, `nohup` scripts) have been completely killed and cleaned up. Do **not** restart them locally unless the user explicitly asks to test locally. The goal is to move to the cloud!

The user has a `gcp_deployment_guide.md` in their `.gemini/antigravity/brain/` folder to guide them through the cloud setup.

## 5. Next Steps
When the user arrives in the new IDE or wants to continue:
1. They will likely be deploying to GCP. Follow the instructions in `gcp_deployment_guide.md` to help them.
2. The ultimate future goal is "Commercialization" (turning this into a multi-tenant SaaS with a Stripe paywall). The SQLite database is already structured with `chat_id` to support this!
