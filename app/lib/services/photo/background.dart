/// Android background backfill via workmanager (spec §6.4 — the nightly
/// `--sync` replacement). iOS: NO-OP by spec delta (§6): share/picker only.
///
/// INTEGRATOR WIRING (read before calling `enableBackgroundScan`):
/// WorkManager starts a FRESH background isolate at the registered
/// dispatcher — it does NOT run `main()`, so nothing assigned in the main
/// isolate is visible there. The integrator must therefore provide a
/// top-level, `@pragma('vm:entry-point')` dispatcher in the composition
/// root that (1) assigns [headlessBackfill] to the fully wired pipeline
/// (DAO + analyzer + a `LibraryPhotoIntake.backfillScan` consumer) and
/// (2) delegates to [handleBackgroundTask], then pass it as the
/// `dispatcher:` argument:
///
///     @pragma('vm:entry-point')
///     void appBackgroundDispatcher() {
///       Workmanager().executeTask((task, input) {
///         headlessBackfill = (days) async { /* wire + scan */ return true; };
///         return handleBackgroundTask(task, input);
///       });
///     }
///     ...
///     await enableBackgroundScan(true, dispatcher: appBackgroundDispatcher);
///
/// The default [callbackDispatcher] is a working skeleton for tests and for
/// integrators that assign [headlessBackfill] some other reachable way.
library;

import 'dart:io' show Platform;

import 'package:flutter/foundation.dart' show visibleForTesting;
import 'package:workmanager/workmanager.dart';

import 'watcher.dart' show clampLookbackDays;

/// Task name WorkManager hands back to the dispatcher.
const String photoBackfillTaskName = 'calorietracker.photo.backfill';

/// Unique name used to register/cancel the ONE periodic backfill task.
const String photoBackfillUniqueName = 'calorietracker.photo.backfill.periodic';

/// Android WorkManager floor: periodic work may not repeat faster than
/// 15 minutes (contract: ">=15min").
const Duration minBackgroundFrequency = Duration(minutes: 15);

/// The headless pipeline entry the integrator assigns (see library docs).
/// Returns WorkManager success; recoverable scan trouble should return true
/// — failed photos stay ledger-'failed' for the retry action (§6.5).
typedef HeadlessBackfill = Future<bool> Function(int lookbackDays);

HeadlessBackfill? headlessBackfill;

/// Default top-level background entrypoint (see library docs for why the
/// integrator usually supplies their own that wires [headlessBackfill]).
@pragma('vm:entry-point')
void callbackDispatcher() {
  Workmanager().executeTask(handleBackgroundTask);
}

/// Pure task handler, separated from the isolate entrypoint so tests can
/// drive it directly.
Future<bool> handleBackgroundTask(
    String task, Map<String, dynamic>? inputData) async {
  if (task != photoBackfillTaskName) return true; // not ours: don't retry
  final lookback = lookbackDaysFromInput(inputData?['lookbackDays']);
  final runner = headlessBackfill;
  if (runner == null) return true; // unwired: succeed, never retry-storm
  try {
    return await runner(lookback);
  } catch (_) {
    // A failed scan leaves recoverable state (ledger keeps 'failed' rows,
    // §6.5); report success so WorkManager doesn't backoff-spiral the chain.
    return true;
  }
}

/// inputData is untrusted across process death: numeric values clamp to the
/// §8 SYNC_LOOKBACK_DAYS range 1–30; anything else → default 2.
int lookbackDaysFromInput(Object? raw) {
  if (raw is num && raw.isFinite) return clampLookbackDays(raw.toInt());
  return 2;
}

/// workmanager seam so tests can observe scheduling without a platform.
abstract class BackgroundScheduler {
  Future<void> initialize(Function dispatcher);
  Future<void> registerPeriodic(String uniqueName, String taskName,
      {required Duration frequency, Map<String, dynamic>? inputData});
  Future<void> cancelByUniqueName(String uniqueName);
}

class WorkmanagerScheduler implements BackgroundScheduler {
  @override
  Future<void> initialize(Function dispatcher) =>
      Workmanager().initialize(dispatcher);

  @override
  Future<void> registerPeriodic(String uniqueName, String taskName,
          {required Duration frequency, Map<String, dynamic>? inputData}) =>
      Workmanager().registerPeriodicTask(uniqueName, taskName,
          frequency: frequency,
          inputData: inputData,
          // Analyses need Gemini; an offline run can only read bytes for
          // nothing (retryable-release keeps it harmless, but don't wake).
          constraints: Constraints(networkType: NetworkType.connected));

  @override
  Future<void> cancelByUniqueName(String uniqueName) =>
      Workmanager().cancelByUniqueName(uniqueName);
}

@visibleForTesting
BackgroundScheduler? debugSchedulerOverride;

@visibleForTesting
bool? debugIsAndroidOverride;

/// Integration-contract registration API. Android only — iOS is a no-op
/// (spec §6 delta: share/picker only on iOS, no background scan).
Future<void> enableBackgroundScan(bool on,
    {int lookbackDays = 2,
    Duration frequency = const Duration(hours: 6),
    Function dispatcher = callbackDispatcher}) async {
  final isAndroid = debugIsAndroidOverride ?? Platform.isAndroid;
  if (!isAndroid) return;
  final scheduler = debugSchedulerOverride ?? WorkmanagerScheduler();
  if (!on) {
    await scheduler.cancelByUniqueName(photoBackfillUniqueName);
    return;
  }
  await scheduler.initialize(dispatcher);
  final period =
      frequency < minBackgroundFrequency ? minBackgroundFrequency : frequency;
  // Cancel-then-register keeps exactly one periodic chain (replace, don't
  // stack) without depending on workmanager's ExistingWorkPolicy surface.
  await scheduler.cancelByUniqueName(photoBackfillUniqueName);
  await scheduler.registerPeriodic(photoBackfillUniqueName,
      photoBackfillTaskName,
      frequency: period,
      inputData: {'lookbackDays': clampLookbackDays(lookbackDays)});
}
