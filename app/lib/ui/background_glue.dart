/// Integrator wiring for the photo module's background half (spec §6.4) —
/// the composition-root counterpart that services/photo/background.dart's
/// library docs demand. Nothing here was wired before 2026-07-23, which is
/// why "automatic" intake only worked while the app was open and unfrozen.
///
/// Three layers of catch-up keep the watcher honest on aggressive OEMs
/// (Honor/EMUI freezes backgrounded apps within minutes):
///   1. Foreground change-notify watcher (di.dart) — instant while open.
///   2. Periodic WorkManager job (this file) — while the app is closed.
///   3. Launch/resume backfill over the FULL lookback window (di.dart +
///      app.dart) — the correctness backstop when 1–2 were killed.
///
/// The periodic job runs in a FRESH isolate each time (empty session
/// dedup), so it scans from a persisted watermark instead of the whole
/// window — otherwise every run would re-read the original bytes of every
/// window photo just to rediscover them in the md5 ledger.
library;

import 'dart:async';

import 'package:flutter/foundation.dart' show visibleForTesting;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:workmanager/workmanager.dart';

import '../core/contracts.dart';
import '../data/meals_dao_impl.dart';
import '../services/analyzer/gemini_analyzer.dart';
import '../services/photo/background.dart';
import '../services/photo/watcher.dart';
import '../services/report/notifications.dart';
import '../services/settings/app_settings.dart';
import 'photo_pipeline.dart';

/// Persisted watermark: start time of the last completed background scan.
const String backgroundWatermarkPrefsKey = 'background.last_scan_iso';

/// Re-scan overlap behind the watermark, absorbing camera write latency and
/// photos created while the previous scan was running.
const Duration backgroundScanOverlap = Duration(hours: 1);

/// Periodic cadence. WorkManager's floor is 15 min; 30 keeps EMUI battery
/// stats friendly while the watermark keeps each run nearly free when
/// nothing new was shot.
const Duration backgroundScanFrequency = Duration(minutes: 30);

/// Result of one drained backfill: what happened to each photo, whether
/// the SCAN itself completed (a scan abort means unseen photos remain),
/// and the intake's coverage frontier (never advance a watermark past it).
class BackfillDrain {
  final List<PhotoOutcome> outcomes;
  final bool scanCompleted;
  final DateTime? frontier;
  const BackfillDrain(this.outcomes,
      {required this.scanCompleted, this.frontier});
}

/// Run one backfill through the pipeline with FULL backpressure: the
/// intake awaits each photo's processing before reading the next photo's
/// bytes (one photo resident at a time — a 200-photo backlog must not hold
/// ~1 GB while the first Gemini call is in flight). When [onOutcome] is
/// set it runs per photo BEFORE the next one starts, so progress
/// (notifications, watermark checkpoints) survives a WorkManager
/// hard-stop mid-batch.
Future<BackfillDrain> drainBackfill(PhotoIntake intake, PhotoPipeline pipeline,
    {int lookbackDays = 0,
    DateTime? since,
    Future<void> Function(
            IntakePhoto photo, PhotoOutcome outcome, DateTime? safeFrontier)?
        onOutcome}) async {
  final outcomes = <PhotoOutcome>[];
  intake.attachSink((p, safeFrontier) async {
    final o = await pipeline.process(p);
    outcomes.add(o);
    if (onOutcome != null) await onOutcome(p, o, safeFrontier);
    return !o.retryable;
  });
  var scanCompleted = false;
  DateTime? frontier;
  try {
    frontier = await intake.backfillScan(
        lookbackDays: lookbackDays, since: since);
    scanCompleted = true;
  } catch (_) {
    // Library query died / window truncated. Photos delivered before the
    // throw are already fully processed (sink is awaited inline); the
    // false flag tells the caller the window was NOT fully covered.
  } finally {
    intake.attachSink(null);
  }
  return BackfillDrain(outcomes,
      scanCompleted: scanCompleted, frontier: frontier);
}

/// The headless scan body, dependency-injected so tests can drive it with
/// fakes. Returns WorkManager success (true) in every recoverable state —
/// background.dart's no-retry-storm rule.
@visibleForTesting
Future<bool> headlessBackfillWith({
  required AppSettings settings,
  required SharedPreferences prefs,
  required PhotoIntake intake,
  required Future<PhotoPipeline> Function() pipeline,
  required Future<void> Function(String title, String body) showMealCard,
  DateTime Function() clock = DateTime.now,
}) async {
  // Belt and braces: disabling the watcher cancels the job, but a stale
  // chain must never scan against the user's setting.
  if (!settings.watcherEnabled) return true;
  // No key yet / quota latch armed: analyses cannot succeed, so don't even
  // read photo bytes. The watermark stays put — those photos are scanned
  // once the blocker clears (release-not-burn keeps them eligible).
  if ((settings.geminiApiKey ?? '').isEmpty) return true;
  if (settings.isQuotaPaused) return true;

  final scanStart = clock();
  final since =
      DateTime.tryParse(prefs.getString(backgroundWatermarkPrefsKey) ?? '');

  Future<void> checkpoint(DateTime mark) async {
    // Monotonic forward only, never past this scan's start.
    final capped = mark.isAfter(scanStart) ? scanStart : mark;
    final cur =
        DateTime.tryParse(prefs.getString(backgroundWatermarkPrefsKey) ?? '');
    if (cur == null || capped.isAfter(cur)) {
      await prefs.setString(
          backgroundWatermarkPrefsKey, capped.toIso8601String());
    }
  }

  // The pipeline (and its database handle) is built ONLY past the guards:
  // most runs guard out or find nothing, and every needless cross-isolate
  // open is a shot at the shared-handle transaction races.
  final drain = await drainBackfill(
    intake,
    await pipeline(),
    since: since?.subtract(backgroundScanOverlap),
    onOutcome: (photo, o, safeFrontier) async {
      // Per photo, BEFORE the next one: a WorkManager hard-stop mid-batch
      // must not lose the notification for an already-saved meal, nor the
      // progress the run already made.
      if (o.kind == PhotoOutcomeKind.saved) {
        await showMealCard('🍽️ Meal logged automatically', o.message);
      }
      // The intake's safeFrontier is the ONLY persistable value: it is
      // createDate-based (the watermark's timebase), halts at transient
      // failures, and is null for truncated windows. Deriving it from the
      // photo (filename dates) skipped photos permanently.
      if (!o.retryable && safeFrontier != null) {
        await checkpoint(safeFrontier);
      }
    },
  );
  // Final watermark: a clean scan with no retryable releases covers the
  // whole window up to scanStart; anything less advances only to the
  // intake's coverage frontier (photos behind a transient failure stay
  // reachable for the next run).
  final anyRetryable = drain.outcomes.any((o) => o.retryable);
  if (drain.scanCompleted && !anyRetryable) {
    await prefs.setString(
        backgroundWatermarkPrefsKey, scanStart.toIso8601String());
  } else if (drain.frontier != null) {
    await checkpoint(drain.frontier!);
  }
  return true;
}

/// Production assembly for one background run. Mirrors di.dart's startup
/// EXCEPT reclaimStaleProcessing: that launch sweep assumes it owns the
/// only pipeline, and the foreground app may be mid-analysis right now —
/// reclaiming its 'processing' reservation here could double-log a meal.
Future<bool> runHeadlessBackfill() async {
  final settings = await AppSettings.load();
  final prefs = await SharedPreferences.getInstance();
  // Time-derived id seed: cards from successive runs stack instead of
  // overwriting each other. Base 10000 keeps the whole range clear of the
  // fixed daily-report id 9001 (a colliding meal card would silently
  // REPLACE an unread daily report).
  final notifier = ReportNotifier(
      mealCardIdSeed: 10000 +
          (DateTime.now().millisecondsSinceEpoch ~/ 10000) % 8640000);
  return headlessBackfillWith(
    settings: settings,
    prefs: prefs,
    intake: createPhotoIntake(settings),
    pipeline: () async => PhotoPipeline(
        dao: await createMealsDao(), analyzer: createAnalyzer(settings)),
    showMealCard: (title, body) async {
      await notifier.init(); // idempotent; deferred until a card exists
      await notifier.showMealCard(title, body);
    },
  );
}

/// WorkManager background entrypoint. Must stay top-level with the
/// vm:entry-point pragma or AOT tree-shaking removes it and every
/// background run dies before Dart (background.dart library docs).
@pragma('vm:entry-point')
void appBackgroundDispatcher() {
  Workmanager().executeTask((task, input) {
    // The registered inputData lookback is ignored on purpose: the runner
    // loads live settings, so slider edits apply without re-registration.
    headlessBackfill = (_) => runHeadlessBackfill();
    return handleBackgroundTask(task, input);
  });
}

/// Reconcile the periodic job with the current settings — call at startup
/// and whenever watcherEnabled/lookbackDays change. Android-only inside
/// (enableBackgroundScan no-ops on iOS: share/picker only by spec §6).
Future<void> syncBackgroundScan(AppSettings settings) =>
    enableBackgroundScan(settings.watcherEnabled,
        lookbackDays: settings.lookbackDays,
        frequency: backgroundScanFrequency,
        dispatcher: appBackgroundDispatcher);
