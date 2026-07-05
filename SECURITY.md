# Security Notes

This project is designed for a private personal deployment. Before publishing a fork or repository publicly, complete this checklist.

## Required Secrets

Store these in `.env` on the server and never commit them:

- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ANDROID_API_KEY`
- `MEAL_RELAY_API_KEY`, if the legacy relay runs with its own key (defaults to `ANDROID_API_KEY`)
- `PUSHPLUS_TOKEN` and `PUSHPLUS_TOPIC`, if used

Generate `ANDROID_API_KEY` with a high-entropy random value, for example:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use the same `ANDROID_API_KEY` in the Android/iOS upload clients.

## Public Repository Checklist

- Copy `.env.example` to `.env` locally and keep `.env` ignored.
- Copy `android/calorie_tracker_upload.example.json` to `~/.calorie_tracker_upload.json` on the phone and set file mode `600`.
- Do not commit exported iOS Shortcuts (`*.shortcut`, `*.wflow`); they may embed upload keys.
- Do not commit `logs/`, `reports/`, `*.db`, `__pycache__/`, or generated photo data.
- Use placeholder launchd plists from this repo; keep machine-specific plists local.
- Rotate any token, bot token, or API key that was ever committed or shared.

## Existing History Warning

This working tree has been cleaned for future commits, but earlier git history may contain runtime logs, reports, personal paths, chat IDs, server IPs, and credentials.

For a public GitHub repository, do one of these:

1. Create a fresh public repository from a clean export of the current working tree, without `.git`.
2. Or rewrite history with a tool such as `git filter-repo` or BFG, then force-push only after rotating every secret that appeared in history.

A normal commit that deletes secrets from the current tree does not remove them from existing git history.

## Network Notes

Phone upload endpoints use `X-API-Key` authentication. Prefer HTTPS or a trusted VPN in front of the server whenever possible; plain HTTP exposes metadata and the upload key to networks between the phone and server.

Set `TRUSTED_PROXY_ENABLED=1` only when a trusted reverse proxy actually sits in front of the upload API; otherwise clients can spoof `X-Forwarded-For` to fake VPN evidence. The bot redacts the Telegram token from its logs and error messages, but treat log files as sensitive anyway.

The legacy `meal_relay.py` binds to `127.0.0.1` by default (override with `MEAL_RELAY_HOST`), requires `X-API-Key` (`MEAL_RELAY_API_KEY`, falling back to `ANDROID_API_KEY`), and rejects request bodies over 2 MB. Do not expose it publicly without a reverse proxy, TLS, and authentication.
