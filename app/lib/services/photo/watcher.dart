/// Camera-roll watcher: the native re-implementation of the Android photo
/// watcher (spec §6).
///
/// Emits [IntakePhoto]s with `deliberate=false` — automated intake NEVER
/// reclaims failed/skipped/deleted ledger rows (spec §2.3 caller policy);
/// the downstream pipeline enforces that via `reservePhotoHash`.
library;

import 'dart:async';

import '../../core/contracts.dart';
import '../settings/app_settings.dart' show AppSettings;
import 'filename_dates.dart';
import 'photo_library.dart';

/// §8: SYNC_LOOKBACK_DAYS range 1–30.
int clampLookbackDays(int days) => days < 1 ? 1 : (days > 30 ? 30 : days);

/// Backfill window math (spec §6.4): include photos whose create-date DAY
/// falls within the last [lookbackDays] days — i.e. the cutoff is local
/// midnight (lookbackDays − 1) days before today, so lookbackDays=2 means
/// today + yesterday exactly (upload_photo.py sync_photos semantics).
DateTime lookbackCutoff(DateTime now, int lookbackDays) {
  final days = clampLookbackDays(lookbackDays);
  return DateTime(now.year, now.month, now.day)
      .subtract(Duration(days: days - 1));
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
  bool _started = false;

  @override
  Stream<IntakePhoto> get photos => _controller.stream;

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
    await _library.stopChangeNotify();
  }

  void _onLibraryChange() {
    unawaited(_incrementalScan());
  }

  Future<void> _incrementalScan() async {
    if (_scanning) return; // change bursts collapse into one scan
    _scanning = true;
    try {
      final cutoff = _incrementalCutoff ?? _clock();
      final assets = await _library.imagesCreatedAfter(cutoff);
      await _emit(assets);
      // Advance only to the newest create-date actually seen; the seen-id
      // set absorbs the deliberate overlap at the cutoff instant.
      for (final asset in assets) {
        final cur = _incrementalCutoff;
        if (cur == null || asset.createDateTime.isAfter(cur)) {
          _incrementalCutoff = asset.createDateTime;
        }
      }
    } finally {
      _scanning = false;
    }
  }

  @override
  Future<void> backfillScan({int lookbackDays = 0}) async {
    // 0 (unset) → the configured §8 default from settings.
    final days = lookbackDays > 0 ? lookbackDays : _lookbackDays();
    final cutoff = lookbackCutoff(_clock(), days);
    // §6.4: EVERY photo in the window is offered downstream; the status
    // ledger (saved/skipped/failed/deleted incl. tombstones) is what makes
    // the re-scan idempotent — keep the window small.
    final assets = await _library.imagesCreatedAfter(cutoff);
    await _emit(assets);
  }

  Future<void> _emit(List<LibraryAsset> assets) async {
    // §6: newest-LAST emission — oldest first, so downstream meal rows land
    // in chronological order.
    final ordered = [...assets]
      ..sort((a, b) => a.createDateTime.compareTo(b.createDateTime));
    for (final asset in ordered) {
      if (_controller.isClosed) return;
      if (!_seenAssetIds.add(asset.id)) continue;
      final bytes = await asset.originBytes(); // ORIGINAL bytes (§6.2)
      if (bytes == null || bytes.isEmpty) {
        _seenAssetIds.remove(asset.id); // transient read failure: retryable
        continue;
      }
      if (bytes.length > maxPhotoBytes) continue; // §8: 25 MB pre-decode cap
      final name = await asset.fileName();
      final capturedAt = deriveCapturedAt(
          fileName: name,
          assetCreateDate: asset.createDateTime,
          now: _clock(),
          maxAgeDays: _capturedAtMaxAgeDays);
      // deliberate=false: automated intake, strict reservation (§2.3).
      _controller.add(IntakePhoto(bytes, asset.id, name,
          capturedAt: capturedAt, deliberate: false));
    }
  }
}
