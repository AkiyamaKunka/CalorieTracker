/// Layer contracts: the seams the feature modules implement and consume.
///
/// These interfaces are the ONLY coupling between data/, services/, and ui/ —
/// each module depends on this file, never on another module's internals.
/// Semantics for every method are specified in docs/APP_PORT_SPEC.md; the
/// section references below are load-bearing.
library;

import 'dart:typed_data';

/// A logged meal row (spec §2: meals table).
class Meal {
  final int id;
  final int chatId; // fixed 1 on-device; kept for schema parity/export
  final String date; // user-local YYYY-MM-DD
  final String time; // user-local hh:mm AM/PM (server format parity)
  final String timestamp; // device clock ISO-8601
  final String source; // 'app_photo' | 'app_watch' | 'manual_text' | ...
  final String imageHash; // md5 of ORIGINAL bytes, '' for text meals
  final String fileId; // local photo asset id / path hint, '' for text
  final Map<String, dynamic> analysis; // non-dict stored JSON coerced to {}
  final bool corrected;

  const Meal({
    required this.id,
    this.chatId = 1,
    required this.date,
    required this.time,
    required this.timestamp,
    required this.source,
    this.imageHash = '',
    this.fileId = '',
    required this.analysis,
    this.corrected = false,
  });
}

/// Photo-ingestion ledger statuses (spec §2/§6). The dedup contract:
/// one row per md5(original bytes); 'saved'/'processing' block re-ingestion,
/// 'failed'/'skipped'/'deleted' are reclaimable by a DELIBERATE user re-add
/// but not by the automatic watcher.
enum IngestionStatus { processing, saved, skipped, failed, deleted }

/// Persistence seam (spec §2, §5 read shapes).
abstract class MealsDao {
  Future<int> saveMeal(Meal meal, {IngestionStatus? markStatus});
  Future<void> updateMealAnalysis(int mealId, Map<String, dynamic> analysis);
  Future<void> deleteMeal(int mealId); // tombstones the ledger row (spec §2)
  Future<List<Meal>> mealsBetween(String startDate, String endDate);
  Future<List<Meal>> recentMeals({int days = 7}); // food-only, oldest-first
  Future<bool> isDuplicatePhoto(String imageHash); // 5-min same-hash window
  /// Reserve the hash before analysis; false = already claimed. Set
  /// [reclaimDeliberate] on user-initiated adds (reclaims failed/skipped/
  /// deleted rows, the "deliberate re-send" rule).
  Future<bool> reservePhotoHash(String imageHash,
      {required String source, bool reclaimDeliberate = false});
  Future<void> markPhotoHash(String imageHash, IngestionStatus status,
      {int? mealId});
  Future<void> releasePhotoHash(String imageHash);
  Future<void> reclaimStaleProcessing(); // launch-time sweep (spec §2 delta)
  // Fitness (phase 1: weight + activity only)
  Future<void> saveBodyWeight(String date, double kg);
  Future<void> saveActivity(String date,
      {num? activeCalories, int? steps, double? distanceKm});
  Future<String> exportJson(); // full data export (spec §8)
}

/// Result of one photo analysis (spec §3).
class AnalysisOutcome {
  final Map<String, dynamic>? analysis; // null = failed (kept for retry)
  final bool isFood; // already coerced via coerceIsFood
  final String? error; // user-facing failure summary
  final Duration wall;
  const AnalysisOutcome(
      {this.analysis, this.isFood = false, this.error, required this.wall});
}

/// Gemini-backed analyzer (spec §3): callers hand ORIGINAL bytes; the
/// implementation normalizes (1568px q85 JPEG), calls generateContent with
/// response_mime_type application/json, applies retry/quota-pause rules, and
/// returns coerced output. Never throws for model/network trouble.
abstract class AnalyzerService {
  Future<AnalysisOutcome> analyzePhoto(Uint8List originalBytes);
  Future<Map<String, dynamic>?> textIntent(String prompt); // raw JSON or null
  Future<String?> validateKey(String apiKey); // null = OK, else error text
}

/// One executed NL action's user-visible result (spec §4).
class NlReply {
  final String text;
  final bool needsDeleteConfirmation;
  final List<int> pendingDeleteIds; // meal ids awaiting the modal confirm
  final List<String> pendingDeleteLabels;
  const NlReply(this.text,
      {this.needsDeleteConfirmation = false,
      this.pendingDeleteIds = const [],
      this.pendingDeleteLabels = const []});
}

/// The hardened NL executor (spec §4): builds the prompt from the meals
/// snapshot, normalizes single/multi/bare-array responses, caps at 5
/// actions, merges duplicate deletes, contains per-action failures, and
/// NEVER deletes without confirmation.
abstract class NlExecutor {
  Future<List<NlReply>> handleText(String userText);
  Future<String> confirmPendingDelete(List<int> mealIds);
}

/// Photo intake events (spec §6). Implementations: Android background watch
/// (photo_manager change notify + periodic backfill scan), share-sheet
/// intake, and in-app picker. captured-at derives from the camera filename
/// regex with the 45-day/+1h validation before falling back to now.
class IntakePhoto {
  final Uint8List bytes;
  final String assetId;
  final String fileName;
  final DateTime? capturedAt;
  final bool deliberate; // user-picked/shared (reclaims failed/skipped rows)
  const IntakePhoto(this.bytes, this.assetId, this.fileName,
      {this.capturedAt, this.deliberate = false});
}

abstract class PhotoIntake {
  Stream<IntakePhoto> get photos;
  Future<void> backfillScan({int lookbackDays});
  Future<void> start();
  Future<void> stop();
}

/// Report/summary builders (spec §5). Pure functions over DAO reads; the
/// notification scheduler consumes dailyReport at the user's chosen time.
abstract class ReportBuilder {
  Future<String> todaySummary();
  Future<String> history({int days = 30});
  Future<String> dailyReport(String date);
  Future<String> stats();
}
