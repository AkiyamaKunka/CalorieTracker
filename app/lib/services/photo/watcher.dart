/// Camera-roll watcher: the native re-implementation of the Android photo
/// watcher (spec §6).
///
/// Emits [IntakePhoto]s with `deliberate=false` — automated intake NEVER
/// reclaims failed/skipped/deleted ledger rows (spec §2.3 caller policy);
/// the downstream pipeline enforces that via `reservePhotoHash`.
library;

import 'dart:async';
import 'dart:typed_data';

import '../../core/contracts.dart';
import '../settings/app_settings.dart' show AppSettings;
import 'filename_dates.dart';
import 'photo_library.dart';

/// §8: SYNC_LOOKBACK_DAYS range 1–30.
int clampLookbackDays(int days) => days < 1 ? 1 : (days > 30 ? 30 : days);

/// Backfill query cap. The library query returns the NEWEST assets first,
/// so hitting the cap means the window's oldest photos were not seen.
const int backfillQueryLimit = 2000;

/// Thrown AFTER emitting a capped result set: everything returned still
/// processes, but callers tracking coverage (the background watermark) must
/// treat the window as not fully scanned.
class BackfillWindowTruncated implements Exception {
  const BackfillWindowTruncated();
  @override
  String toString() =>
      'BackfillWindowTruncated: >$backfillQueryLimit images in the window';
}

/// Backfill window math (spec §6.4): include photos whose create-date DAY
/// falls within the last [lookbackDays] days — i.e. the cutoff is local
/// midnight (lookbackDays − 1) days before today, so lookbackDays=2 means
/// today + yesterday exactly (upload_photo.py sync_photos semantics).
/// Calendar-component arithmetic, NOT Duration subtraction: a Duration is
/// 24 absolute hours, which lands an hour off local midnight across a DST
/// transition (the device may travel even though China skips DST).
DateTime lookbackCutoff(DateTime now, int lookbackDays) {
  final days = clampLookbackDays(lookbackDays);
  return DateTime(now.year, now.month, now.day - (days - 1));
}

/// Integration-contract factory: the integrator wires this with the
/// settings module's [AppSettings]. The lookback window is read through a
/// closure so later settings edits apply to the next scan without rewiring.
PhotoIntake createPhotoIntake(AppSettings settings) => LibraryPhotoIntake(
    lookbackDays: () => settings.lookbackDays,
    library: PhotoManagerLibrary());

class LibraryPhotoIntake implements PhotoIntake {
  LibraryPhotoIntake(
      {required this._lookbackDays,
      required this._library,
      DateTime Function()? clock,
      // §8 CAPTURED_AT_MAX_AGE_DAYS: default 45, not user-editable.
      this._capturedAtMaxAgeDays = 45})
      : _clock = clock ?? DateTime.now;

  final int Function() _lookbackDays;
  final PhotoLibrary _library;
  final DateTime Function() _clock;
  final int _capturedAtMaxAgeDays;
  final StreamController<IntakePhoto> _controller =
      StreamController<IntakePhoto>.broadcast();
  // Session-scoped emission dedup; cross-session idempotency belongs to the
  // md5 ledger (spec §6.2/§2.3), not this set.
  final Set<String> _seenAssetIds = <String>{};
  DateTime? _incrementalCutoff;
  bool _scanning = false;
  bool _rescanPending = false;
  bool _started = false;
  Future<bool> Function(IntakePhoto, DateTime?)? _sink;
  // Asset ids whose delivery is still awaiting the sink: a CONCURRENT scan
  // must not count them as covered — their release may still be coming.
  final Set<String> _inFlightIds = <String>{};
  // stop() bumps this; in-flight emission loops observe the change and
  // quiesce instead of auto-logging on after the user disabled the watcher.
  int _generation = 0;

  @override
  Stream<IntakePhoto> get photos => _controller.stream;

  @override
  void attachSink(
      Future<bool> Function(IntakePhoto photo, DateTime? safeFrontier)?
          sink) {
    _sink = sink;
  }

  @override
  Future<void> start() async {
    if (_started) return;
    // §6 permission flow: denial is not an error — share/picker intake still
    // works; the watcher just stays silent.
    if (!await _library.requestPermission()) return;
    _incrementalCutoff = _clock();
    await _library.startChangeNotify(_onLibraryChange);
    _started = true;
  }

  @override
  Future<void> stop() async {
    if (!_started) return;
    _started = false;
    _generation++; // quiesce in-flight emission loops
    await _library.stopChangeNotify();
  }

  void _onLibraryChange() {
    unawaited(_incrementalScan());
  }

  Future<void> _incrementalScan() async {
    if (_scanning) {
      // A scan with a bound sink can run MINUTES (one Gemini call per
      // photo) — dropping this event outright would blind the live
      // watcher to every photo taken mid-scan. Mark dirty and re-scan
      // when the current pass finishes.
      _rescanPending = true;
      return;
    }
    _scanning = true;
    try {
      final cutoff = _incrementalCutoff ?? _clock();
      final assets = await _library.imagesCreatedAfter(cutoff);
      final frontier = await _emit(assets);
      // Advance ONLY over the contiguous run of successfully-covered
      // photos (oldest-first), and never past the wall clock: a transient
      // read failure must stay inside the window so the next change event
      // retries it, and one future-skewed createDate (camera clock drift)
      // must not blind the live watcher for the skew duration.
      if (frontier != null) {
        final now = _clock();
        final capped = frontier.isAfter(now) ? now : frontier;
        final cur = _incrementalCutoff;
        if (cur == null || capped.isAfter(cur)) _incrementalCutoff = capped;
      }
    } catch (_) {
      // Change-notify runs unawaited: a library query failure must not
      // become an unhandled zone error. The cutoff didn't advance, so the
      // next change event retries the same window.
    } finally {
      _scanning = false;
      if (_rescanPending) {
        _rescanPending = false;
        if (_started) unawaited(_incrementalScan());
      }
    }
  }

  @override
  Future<DateTime?> backfillScan(
      {int lookbackDays = 0, DateTime? since}) async {
    // 0 (unset) → the configured §8 default from settings.
    final days = lookbackDays > 0 ? lookbackDays : _lookbackDays();
    var cutoff = lookbackCutoff(_clock(), days);
    // A watermark can only NARROW the window, never widen it past §8's
    // lookback clamp.
    if (since != null && since.isAfter(cutoff)) cutoff = since;
    // §6.4: EVERY photo in the window is offered downstream; the status
    // ledger (saved/skipped/failed/deleted incl. tombstones) is what makes
    // the re-scan idempotent — keep the window small. The raised limit
    // keeps a heavy-shooter window from being truncated newest-first
    // (a truncated scan under a watermark skips the tail forever).
    final assets =
        await _library.imagesCreatedAfter(cutoff, limit: backfillQueryLimit);
    // Truncation is known BEFORE emission (query hit its cap): the unseen
    // tail is OLDER than everything returned, so no frontier — per-photo
    // or final — may be reported for this scan.
    final truncated = assets.length >= backfillQueryLimit;
    final frontier = await _emit(assets, reportFrontier: !truncated);
    // Signal AFTER emitting: the caller processes what was seen, but a
    // watermark must not advance over the unseen tail.
    if (truncated) throw const BackfillWindowTruncated();
    return frontier;
  }

  /// Delivers [assets] oldest-first and returns the coverage FRONTIER: the
  /// newest createDate such that every asset at or before it was terminally
  /// handled (delivered, seen before, or permanently skipped). The frontier
  /// halts at the first transient failure or retryable release so callers
  /// never advance a cutoff/watermark past a photo that still needs a
  /// retry. Null = nothing covered.
  /// [reportFrontier] false (truncated window): every delivery carries
  /// safeFrontier null — the unseen OLDER tail forbids any watermark claim.
  Future<DateTime?> _emit(List<LibraryAsset> assets,
      {bool reportFrontier = true}) async {
    // §6: newest-LAST emission — oldest first, so downstream meal rows land
    // in chronological order.
    final ordered = [...assets]
      ..sort((a, b) => a.createDateTime.compareTo(b.createDateTime));
    final generation = _generation;
    DateTime? frontier;
    var frontierOpen = true;

    void cover(LibraryAsset asset) {
      if (frontierOpen) frontier = asset.createDateTime;
    }

    for (final asset in ordered) {
      if (_controller.isClosed) break;
      if (_generation != generation) break; // stop(): quiesce mid-batch
      if (!_seenAssetIds.add(asset.id)) {
        if (_inFlightIds.contains(asset.id)) {
          // A concurrent scan is still delivering it — its release may be
          // coming; nothing may pass it yet.
          frontierOpen = false;
        } else {
          cover(asset); // already terminally handled this session
        }
        continue;
      }
      final Uint8List? bytes;
      final String name;
      try {
        bytes = await asset.originBytes(); // ORIGINAL bytes (§6.2)
        if (bytes == null || bytes.isEmpty) {
          _seenAssetIds.remove(asset.id); // transient read failure: retryable
          frontierOpen = false;
          continue;
        }
        if (bytes.length > maxPhotoBytes) {
          cover(asset); // §8 25 MB pre-decode cap: permanent skip
          continue;
        }
        name = await asset.fileName();
      } catch (_) {
        // photo_manager REPLIES an error (not null) for unreadable/deleted
        // assets on Android 11+ — one bad asset must skip, not abort the
        // whole scan (a scan abort loses every photo behind it AND, in the
        // background job, kills the run before its watermark logic).
        _seenAssetIds.remove(asset.id);
        frontierOpen = false;
        continue;
      }
      final capturedAt = deriveCapturedAt(
          fileName: name,
          assetCreateDate: asset.createDateTime,
          now: _clock(),
          maxAgeDays: _capturedAtMaxAgeDays);
      // deliberate=false: automated intake, strict reservation (§2.3).
      final photo = IntakePhoto(bytes, asset.id, name,
          capturedAt: capturedAt, deliberate: false);
      final sink = _sink;
      if (sink != null) {
        // Backpressured path: the next asset's bytes are read only after
        // this photo fully processed — one photo resident at a time. The
        // safeFrontier is what a consumer may persist if THIS photo
        // terminates: createDate-based and halted at any earlier failure.
        final safe = (reportFrontier && frontierOpen)
            ? asset.createDateTime
            : (reportFrontier ? frontier : null);
        _inFlightIds.add(asset.id);
        final bool terminal;
        try {
          terminal = await sink(photo, safe);
        } finally {
          _inFlightIds.remove(asset.id);
        }
        if (terminal) {
          cover(asset);
        } else {
          _seenAssetIds.remove(asset.id); // released: re-offer next scan
          frontierOpen = false;
        }
      } else {
        _controller.add(photo);
        cover(asset);
      }
    }
    return frontier;
  }
}
