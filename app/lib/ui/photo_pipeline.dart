/// Photo intake → ledger → analyzer → meal glue (spec §2.3 reservation
/// contract, §3.5 persist sanitization, §6 intake rules).
///
/// Per-photo errors are contained (mark `failed`, keep the stream alive) —
/// spec §6 ledger rules: every reservation must end saved/skipped/failed.
library;

import 'dart:async';

import 'dart:typed_data';

import 'package:flutter/foundation.dart' show compute;
import 'package:intl/intl.dart';

import '../core/coerce.dart';
import '../core/contracts.dart';
import '../services/analyzer/normalize.dart' show makeMealThumb;
import '../services/photo/filename_dates.dart'
    show exifCapturedAt, validateCapturedAt;
import '../services/photo/photo_hash.dart' show originalBytesMd5;
import 'format.dart';

Future<String> _computeMd5(Uint8List bytes) =>
    compute(originalBytesMd5, bytes);

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

  /// Called after a meal row lands, so open screens can re-query. Separate
  /// from [notify] on purpose: notify also fires for failures, and only a
  /// SAVE changes what the lists should show.
  final void Function()? onMealSaved;

  final List<StreamSubscription<IntakePhoto>> _subs = [];

  // FIFO tail: bound-stream photos process ONE at a time. A backfill batch
  // fired concurrently self-inflicts Gemini RPM 429s (free tier) and piles
  // write contention onto the ledger; the server processed sequentially too.
  // process() never throws (contract), so the chain cannot break.
  Future<void> _tail = Future.value();

  PhotoPipeline(
      {required this.dao,
      required this.analyzer,
      this.notify,
      this.onMealSaved,
      Future<String> Function(Uint8List bytes)? hasher})
      : _hash = hasher ?? _computeMd5;

  /// Injectable so tests stay synchronous; production hashes on a worker.
  final Future<String> Function(Uint8List bytes) _hash;

  /// Wire the watcher intake with BACKPRESSURE: the intake awaits each
  /// photo's turn through the same global FIFO the share stream uses, so
  /// byte reads never run ahead of processing (one photo's bytes resident
  /// at a time). Returns non-retryable to the intake so released photos
  /// are re-offered by later scans.
  void bind(PhotoIntake intake) {
    intake.attachSink((p, _) {
      final slot = _tail.then((_) => process(p));
      _tail = slot.then((_) {});
      return slot.then((o) => !o.retryable);
    });
  }

  /// FIFO-honest single-photo entry: chains onto the same global tail the
  /// watcher sink and share stream use, so callers (coverage Log/Retry)
  /// QUEUE BEHIND — never race — an in-flight scan. Calling process()
  /// directly would run a second analyzer call concurrently (free-tier RPM
  /// 429s) and hold two originals in memory.
  Future<PhotoOutcome> enqueue(IntakePhoto photo) {
    final slot = _tail.then((_) => process(photo));
    _tail = slot.then((_) {});
    return slot;
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
    // OFF the UI isolate: md5 over a 12-25 MB original is tens of ms of
    // straight-line work, and an album burst runs this once per photo —
    // visible jank while the user scrolls (the coverage auditor already
    // hashes via compute for exactly this reason).
    //
    // INSIDE the try: compute() spawns an isolate and can fail on a
    // low-RAM device, and process() is contractually non-throwing — a
    // throw here would poison the shared _tail future and silently stop
    // ALL later photos until an app restart.
    // Non-final: the catch block reads it, and a hash failure leaves it
    // empty (nothing was reserved under it, so nothing to mark).
    var hash = '';
    var reserved = false;
    try {
      hash = normalizeImageHash(await _hash(photo.bytes));
      // Pre-reservation 5-minute duplicate window — deliberate (user-facing)
      // path only, matching the server's chat-only check (spec §2.3).
      if (photo.deliberate && await dao.isDuplicatePhoto(hash)) {
        return const PhotoOutcome(PhotoOutcomeKind.duplicate,
            'Looks like a duplicate of a photo logged minutes ago.');
      }

      // Reservation: deliberate adds reclaim failed/skipped/deleted rows;
      // automated watch intake is strict (spec §2.3 caller policies).
      final source =
          photo.deliberate ? MealSource.appPhoto : MealSource.appWatch;
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
      // When BOTH §6.3 sources failed (share-sheet photo, no timestamp in
      // the filename), the JPEG's own EXIF shutter time is the last truth
      // before intake-time dating — same validation window (§9 app-only,
      // 2026-07-31). This is what keeps a 23:50 photo shared after
      // midnight on YESTERDAY's total.
      // validateCapturedAt is NOT optional here: EXIF is attacker- and
      // junk-controlled (a 2015 stock photo, a camera with a dead clock,
      // a forward-set date), and an unvalidated value writes a meal into
      // a random month of the user's log where they will never find it.
      final when = photo.capturedAt ??
          validateCapturedAt(exifCapturedAt(photo.bytes),
              now: DateTime.now()) ??
          DateTime.now();
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
      // History thumbnail from the ORIGINAL bytes we already hold (spec §9
      // app-only). Best-effort AND awaited: the pipeline is serialized, so
      // finishing the thumb before the next photo keeps memory flat; any
      // failure must never un-save the meal.
      try {
        final thumb = await compute(makeMealThumb, photo.bytes);
        if (thumb != null) await dao.saveMealThumb(id, thumb);
      } catch (_) {}
      final summary =
          '${mealDescription(analysis)} — ~${displayTotalCalories(analysis)} kcal';
      // Same shield as the thumbnail: these run AFTER the commit, so a
      // throwing callback must not fall into the outer catch — that
      // flipped the committed ledger row saved→failed and told the user
      // the intake failed (pressure-test find, 2026-08-03).
      try {
        notify?.call('Meal logged: $summary');
        onMealSaved?.call();
      } catch (_) {}
      // No row id in the copy: the chat flow numbers meals by LIST position
      // ("meal 2 was roast duck"), so surfacing the SQLite id taught users
      // a number that is guaranteed to miss.
      return PhotoOutcome(PhotoOutcomeKind.saved, 'Meal logged: $summary',
          analysis: analysis);
    } catch (e) {
      // Containment: never rethrow (spec §6). Mark failed ONLY if we hold
      // the reservation — a pre-reservation throw (e.g. the duplicate
      // check or reserve itself, typically transient DB contention) must
      // not burn an untracked hash into 'failed'. It also leaves NO ledger
      // row, so report it RETRYABLE: the frontier then halts and a later
      // scan re-offers the photo instead of the watermark passing over an
      // unrecorded one.
      if (reserved) {
        try {
          await dao.markPhotoHash(hash, IngestionStatus.failed);
        } catch (_) {}
      }
      return PhotoOutcome(PhotoOutcomeKind.failed, 'Photo intake failed: $e',
          retryable: !reserved);
    }
  }
}
