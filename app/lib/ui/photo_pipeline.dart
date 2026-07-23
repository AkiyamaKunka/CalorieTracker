/// Photo intake → ledger → analyzer → meal glue (spec §2.3 reservation
/// contract, §3.5 persist sanitization, §6 intake rules).
///
/// Per-photo errors are contained (mark `failed`, keep the stream alive) —
/// spec §6 ledger rules: every reservation must end saved/skipped/failed.
library;

import 'dart:async';

import 'package:crypto/crypto.dart';
import 'package:intl/intl.dart';

import '../core/coerce.dart';
import '../core/contracts.dart';
import 'format.dart';

enum PhotoOutcomeKind { saved, skipped, failed, duplicate, alreadyTracked }

class PhotoOutcome {
  final PhotoOutcomeKind kind;
  final Map<String, dynamic>? analysis; // present when kind == saved

  /// True when the failure was transient and the RESERVATION WAS RELEASED —
  /// the photo stays eligible and a later scan re-offers it. Consumers use
  /// this to hold watermarks/seen-sets open instead of marking coverage.
  final bool retryable;
  final String message; // user-facing summary
  const PhotoOutcome(this.kind, this.message,
      {this.analysis, this.retryable = false});
}

class PhotoPipeline {
  final MealsDao dao;
  final AnalyzerService analyzer;

  /// Notification-style surface (snackbar) for background saves.
  final void Function(String message)? notify;

  final List<StreamSubscription<IntakePhoto>> _subs = [];

  // FIFO tail: bound-stream photos process ONE at a time. A backfill batch
  // fired concurrently self-inflicts Gemini RPM 429s (free tier) and piles
  // write contention onto the ledger; the server processed sequentially too.
  // process() never throws (contract), so the chain cannot break.
  Future<void> _tail = Future.value();

  PhotoPipeline({required this.dao, required this.analyzer, this.notify});

  /// Wire the watcher intake with BACKPRESSURE: the intake awaits each
  /// photo's turn through the same global FIFO the share stream uses, so
  /// byte reads never run ahead of processing (one photo's bytes resident
  /// at a time). Returns non-retryable to the intake so released photos
  /// are re-offered by later scans.
  void bind(PhotoIntake intake) {
    intake.attachSink((p) {
      final slot = _tail.then((_) => process(p));
      _tail = slot.then((_) {});
      return slot.then((o) => !o.retryable);
    });
  }

  /// Also used for the share-sheet intake stream (deliberate adds, spec §6).
  void bindStream(Stream<IntakePhoto> photos) {
    _subs.add(photos.listen((p) {
      _tail = _tail.then((_) => process(p));
    }, onError: (Object _) {}));
  }

  Future<void> dispose() async {
    for (final s in _subs) {
      await s.cancel();
    }
    _subs.clear();
  }

  /// One photo through the full pipeline. Never throws.
  Future<PhotoOutcome> process(IntakePhoto photo) async {
    // Identity = md5 of ORIGINAL bytes, normalized (spec §6.2, §2.2).
    final hash = normalizeImageHash(md5.convert(photo.bytes).toString());
    var reserved = false;
    try {
      // Pre-reservation 5-minute duplicate window — deliberate (user-facing)
      // path only, matching the server's chat-only check (spec §2.3).
      if (photo.deliberate && await dao.isDuplicatePhoto(hash)) {
        return const PhotoOutcome(PhotoOutcomeKind.duplicate,
            'Looks like a duplicate of a photo logged minutes ago.');
      }

      // Reservation: deliberate adds reclaim failed/skipped/deleted rows;
      // automated watch intake is strict (spec §2.3 caller policies).
      final source = photo.deliberate ? 'app_photo' : 'app_watch';
      reserved = await dao.reservePhotoHash(hash,
          source: source, reclaimDeliberate: photo.deliberate);
      if (!reserved) {
        return const PhotoOutcome(PhotoOutcomeKind.alreadyTracked,
            'This photo was already logged.');
      }

      final outcome = await analyzer.analyzePhoto(photo.bytes);
      if (outcome.analysis == null) {
        final why = outcome.error ?? 'Analysis failed.';
        if (outcome.retryable) {
          // Transient trouble (rate limit / network / quota pause / no key):
          // RELEASE the reservation so the next scan simply re-offers the
          // photo. Burning it to 'failed' would drop it forever on the
          // automated path (watch intake never reclaims, spec §2.3).
          await dao.releasePhotoHash(hash);
          return PhotoOutcome(PhotoOutcomeKind.failed, why, retryable: true);
        }
        // Permanent failure: kept as status failed for deliberate retry
        // (spec §6.5).
        await dao.markPhotoHash(hash, IngestionStatus.failed);
        notify?.call('Photo analysis failed — kept for retry. $why');
        return PhotoOutcome(PhotoOutcomeKind.failed, why);
      }
      if (!outcome.isFood) {
        // Tombstone so backfill never re-analyzes it (spec §6.4).
        await dao.markPhotoHash(hash, IngestionStatus.skipped);
        return const PhotoOutcome(
            PhotoOutcomeKind.skipped, 'No food detected in this photo.');
      }

      final analysis = Map<String, dynamic>.from(outcome.analysis!);
      if (analysis.containsKey('food_items')) {
        // Sanitize before persisting — a hostile shape is never stored
        // (spec §3.5).
        analysis['food_items'] = safeFoodItems(analysis);
      }

      // Backdating: a valid capturedAt (validated by the intake module,
      // spec §6.3) sets date/time; timestamp stays now (spec §2.2).
      final when = photo.capturedAt ?? DateTime.now();
      final id = await dao.saveMeal(
        Meal(
          id: 0, // assigned by the DAO on insert
          date: isoDate(when),
          // 12-hour zero-padded hh:mm AM/PM, server parity (spec §2.2).
          time: DateFormat('hh:mm a').format(when),
          timestamp: DateTime.now().toIso8601String(),
          source: source,
          imageHash: hash,
          fileId: photo.assetId,
          analysis: analysis,
        ),
        markStatus: IngestionStatus.saved, // atomic saved mark (spec §2.3)
      );
      final summary =
          '${mealDescription(analysis)} — ~${displayTotalCalories(analysis)} kcal';
      notify?.call('Meal logged: $summary');
      return PhotoOutcome(PhotoOutcomeKind.saved, 'Logged meal #$id: $summary',
          analysis: analysis);
    } catch (e) {
      // Containment: never rethrow (spec §6). Mark failed ONLY if we hold
      // the reservation — a pre-reservation throw (e.g. the duplicate
      // check) must not burn an untracked hash into 'failed', which the
      // automated path would then never reclaim.
      if (reserved) {
        try {
          await dao.markPhotoHash(hash, IngestionStatus.failed);
        } catch (_) {}
      }
      return PhotoOutcome(PhotoOutcomeKind.failed, 'Photo intake failed: $e');
    }
  }
}
