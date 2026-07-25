/// Manual photo-coverage audit (spec §9 app-only): "has every photo in the
/// window been through intake?"
///
/// The check is md5-based like all photo identity (spec §6.2): each library
/// photo's ORIGINAL bytes are hashed and looked up in the ingestion ledger.
/// That means the audit reads bytes (unavoidable — identity IS the bytes)
/// but never calls a model: it only CLASSIFIES. Acting on the result
/// (logging missing photos, retrying failed ones) goes through the normal
/// pipeline, one photo at a time.
library;

// ignore_for_file: prefer_initializing_formals

import '../../core/contracts.dart';
import 'filename_dates.dart';
import 'photo_library.dart';
import 'watcher.dart'
    show backfillQueryLimit, clampLookbackDays, lookbackCutoff;

/// One photo the audit could not account for (or that needs the user).
class CoverageItem {
  const CoverageItem({
    required this.assetId,
    required this.fileName,
    required this.createDate,
    this.mealId,
  });

  final String assetId;
  final String fileName;
  final DateTime createDate;

  /// For logged photos: the meal the hash resolved to.
  final int? mealId;
}

class CoverageReport {
  const CoverageReport({
    required this.windowDays,
    required this.scanned,
    required this.logged,
    required this.skippedNonFood,
    required this.failed,
    required this.deleted,
    required this.inFlight,
    required this.missing,
    required this.unreadable,
    required this.truncated,
  });

  final int windowDays;

  /// Photos the library returned inside the window.
  final int scanned;

  /// Hash ledgered as saved → a meal exists.
  final List<CoverageItem> logged;

  /// Ledgered skipped (model said not food) — informational.
  final int skippedNonFood;

  /// Ledgered failed — eligible for a deliberate retry (spec §6.5).
  final List<CoverageItem> failed;

  /// Ledgered deleted (user removed the meal) — deliberately NOT offered
  /// for re-logging in bulk: the user already decided about these.
  final int deleted;

  /// Ledgered processing — an analysis is (or was) in flight.
  final int inFlight;

  /// No ledger row at all: intake NEVER saw this photo. The reason this
  /// feature exists.
  final List<CoverageItem> missing;

  /// Bytes could not be read (cloud-offloaded, permission edge, corrupt).
  final int unreadable;

  /// The library query hit its cap — older photos in the window were not
  /// examined and the report cannot promise completeness.
  final bool truncated;

  bool get fullyCovered => !truncated && missing.isEmpty && unreadable == 0;
}

/// Digest function seam (production: md5 over original bytes, normalized).
typedef PhotoHasher = String Function(List<int> bytes);

class CoverageAuditor {
  CoverageAuditor({
    required PhotoLibrary library,
    required MealsDao dao,
    required PhotoHasher hasher,
    DateTime Function()? clock,
  })  : _library = library,
        _dao = dao,
        _hasher = hasher,
        _clock = clock ?? DateTime.now;

  final PhotoLibrary _library;
  final MealsDao _dao;
  final PhotoHasher _hasher;
  final DateTime Function() _clock;

  /// Sequential sweep: one photo's bytes resident at a time (the same
  /// backpressure lesson as the intake path — a parallel map would hold the
  /// whole window's originals in memory).
  Future<CoverageReport> audit({
    required int lookbackDays,
    void Function(int done, int total)? onProgress,
  }) async {
    final days = clampLookbackDays(lookbackDays);
    final cutoff = lookbackCutoff(_clock(), days);
    final assets =
        await _library.imagesCreatedAfter(cutoff, limit: backfillQueryLimit);
    final truncated = assets.length >= backfillQueryLimit;

    final logged = <CoverageItem>[];
    final failed = <CoverageItem>[];
    final missing = <CoverageItem>[];
    var skipped = 0, deleted = 0, inFlight = 0, unreadable = 0, done = 0;

    for (final asset in assets) {
      onProgress?.call(done++, assets.length);
      final bytes = await asset.originBytes();
      if (bytes == null || bytes.isEmpty) {
        unreadable++;
        continue;
      }
      String name;
      try {
        name = await asset.fileName();
      } catch (_) {
        name = '';
      }
      final item = CoverageItem(
        assetId: asset.id,
        fileName: name,
        createDate: asset.createDateTime,
      );
      final row = await _dao.photoStatus(_hasher(bytes));
      if (row == null) {
        // Oversize photos are refused by intake by DESIGN (spec §8 25 MB
        // cap) — report them as covered-by-policy, not as missing.
        if (bytes.length > maxPhotoBytes) {
          skipped++;
        } else {
          missing.add(item);
        }
        continue;
      }
      switch (row.status) {
        case IngestionStatus.saved:
          logged.add(CoverageItem(
            assetId: asset.id,
            fileName: name,
            createDate: asset.createDateTime,
            mealId: row.mealId,
          ));
        case IngestionStatus.skipped:
          skipped++;
        case IngestionStatus.failed:
          failed.add(item);
        case IngestionStatus.deleted:
          deleted++;
        case IngestionStatus.processing:
          inFlight++;
      }
    }
    onProgress?.call(assets.length, assets.length);
    return CoverageReport(
      windowDays: days,
      scanned: assets.length,
      logged: logged,
      skippedNonFood: skipped,
      failed: failed,
      deleted: deleted,
      inFlight: inFlight,
      missing: missing,
      unreadable: unreadable,
      truncated: truncated,
    );
  }

  /// Re-fetch one audited photo as an [IntakePhoto] for processing.
  /// [deliberate] true: the user explicitly asked to log/retry it — the
  /// reservation may reclaim failed rows (spec §2.3 deliberate re-send).
  Future<IntakePhoto?> loadForProcessing(CoverageItem item,
      {bool deliberate = true}) async {
    final assets = await _library.imagesCreatedAfter(
      item.createDate.subtract(const Duration(seconds: 1)),
      limit: backfillQueryLimit,
    );
    for (final asset in assets) {
      if (asset.id != item.assetId) continue;
      final bytes = await asset.originBytes();
      if (bytes == null || bytes.isEmpty || bytes.length > maxPhotoBytes) {
        return null;
      }
      final capturedAt = deriveCapturedAt(
        fileName: item.fileName,
        assetCreateDate: asset.createDateTime,
        now: _clock(),
        maxAgeDays: 45,
      );
      return IntakePhoto(bytes, asset.id, item.fileName,
          capturedAt: capturedAt, deliberate: deliberate);
    }
    return null; // asset vanished between audit and action
  }
}
