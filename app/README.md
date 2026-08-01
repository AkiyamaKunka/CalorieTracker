# CalorieTracker Mobile (iOS + Android)

The on-device edition of CalorieTracker: a Flutter app that implements the
bot's feature set locally — photo → Gemini analysis → meal log, natural-
language corrections, daily summaries — with **no server, no Telegram, no
accounts**. Meals live in a local SQLite database; the only credential is
your own Gemini API key, stored in the platform secure keystore.

Behavior is ported from the Python bot against [docs/APP_PORT_SPEC.md](../docs/APP_PORT_SPEC.md)
(prompts verbatim, same coercion rules, same hardened NL-executor semantics,
same image normalization: 1568px q85 JPEG). 801 Dart tests mirror the
server suite's pinned behaviors, including the compound correction+delete
shape that once crash-looped production.

## Features (phase 1)

- **Photo → meal card**: pick/share a photo (both OSes) or enable the
  camera-roll watcher (Android background scan; iOS scans on app open).
  Original bytes are md5-deduped through the same ingestion ledger as the
  server; capture dates come from camera filenames (`IMG_YYYYMMDD_HHMMSS`)
  with the 45-day/+1h validation, so backfilled photos land on the day
  they were taken.
- **Natural-language box**: "change lunch to 500 kcal", "第二顿是烧鸭饭
  删除第三顿" — corrections, deletes (always confirmation-gated), new text
  meals, weight and activity logging, compound instructions.
- **Today / History**: running totals with the typical-day median line,
  30-day history, all-time stats; daily summary notification at your
  chosen time.
- **Export**: full JSON export via the share sheet.

## Build & run

```bash
cd app
flutter pub get
flutter test                      # 801 tests
flutter build apk --debug         # Android APK
flutter build ios --simulator     # iOS (simulator)
```

- **Android device**: `flutter install` with the phone attached (or adb
  install the APK from `build/app/outputs/flutter-apk/`).
- **iOS device**: open `ios/Runner.xcworkspace` in Xcode once to set your
  signing team, then `flutter run`.
- On this Mac, if `flutter test`/iOS builds complain about CommandLineTools,
  run once: `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`.
- Mainland-China networks: the Gradle configs already route through Aliyun
  Maven mirrors.

## Known limits (v1)

- iOS has no background photo watching (platform restriction) — share to
  the app or open it; the backfill scan catches up.
- iOS share-sheet support runs through the system share intent; a native
  Share Extension target is a future enhancement.
- The daily notification re-arms while the app is alive; exact delivery
  after force-kill needs the platform alarm route (planned).
- Fitness suite (diet modes, VDOT training, Garmin) is spec'd
  (APP_PORT_SPEC §7) but not yet ported.
- Analysis is Gemini-only on device (the server's Claude-subscription
  analyzer requires the desktop CLI).
