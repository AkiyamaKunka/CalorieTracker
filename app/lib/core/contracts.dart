/// Layer contracts: the seams the feature modules implement and consume.
///
/// These interfaces are the ONLY coupling between data/, services/, and ui/ —
/// each module depends on this file, never on another module's internals.
/// Semantics for every method are specified in docs/APP_PORT_SPEC.md; the
/// section references below are load-bearing.
library;

import 'dart:typed_data';

/// The `source` column's vocabulary. These strings are WIRE VALUES: they
/// are persisted in `meals.source` / `photo_ingestions.source`, exported by
/// exportJson, and MATCHED IN SQL — the reclaim sweep releases only
/// [appWatch] rows (spec §9). A literal typo in any one call site silently
/// changes intake policy rather than failing loudly, which is why they live
/// here instead of being spelled out at each use.
abstract final class MealSource {
  /// Deliberate photo add: in-app picker or the share sheet. Reclaims
  /// failed/skipped/deleted ledger rows (spec §2.3 deliberate re-send).
  static const String appPhoto = 'app_photo';

  /// Automated camera-roll intake (watcher, background job, catch-up scans).
  /// The strict reservation path — never reclaims.
  static const String appWatch = 'app_watch';

  /// A meal parsed from a TEXT description — the describe-a-meal screen and
  /// the NL new_meal intent. Server parity: telegram_bot uses this string
  /// for the same case (spec §4.6).
  static const String manualText = 'manual_text';

  /// Numbers typed by hand in the meal editor, no description parsed and no
  /// photo involved. Distinct from [manualText] on purpose: it records that
  /// NO model produced these values.
  static const String appManual = 'app_manual';

  /// Hand-entered numbers for a photo the model REFUSED (the coverage
  /// screen's "log it yourself" path): a photo exists and its md5 is
  /// ledgered, but every number came from the user.
  static const String appManualPhoto = 'app_manual_photo';
}

/// A logged meal row (spec §2: meals table).
class Meal {
  final int id;
  final int chatId; // fixed 1 on-device; kept for schema parity/export
  final String date; // user-local YYYY-MM-DD
  final String time; // user-local hh:mm AM/PM (server format parity)
  final String timestamp; // device clock ISO-8601
  final String source; // one of MealSource.* (plus imported/legacy values)
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

  /// Editor save (spec §9): rewrites the analysis AND optionally moves the
  /// meal's local date/time — a mis-dated meal must be fixable in place
  /// rather than by delete + re-add (which would burn the photo's ledger
  /// row to 'deleted' and lose the original intake provenance). Sets
  /// corrected=1 like [updateMealAnalysis].
  Future<void> updateMealFields(int mealId,
      {Map<String, dynamic>? analysis, String? date, String? time});
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

  /// Read-only ledger lookup for the coverage audit: the status of one
  /// photo hash (and the meal it became, when saved). Null = never seen —
  /// the photo has NOT been through intake.
  Future<({IngestionStatus status, int? mealId})?> photoStatus(
      String imageHash);

  /// Tiny per-meal JPEG (spec §9 app-only): written by the pipeline at
  /// save time, deleted with the meal, shown beside the analysis so the
  /// user can check the numbers against the actual photo.
  Future<void> saveMealThumb(int mealId, Uint8List jpeg);
  Future<Uint8List?> mealThumb(int mealId);
  // Fitness (phase 1: weight + activity only)
  /// One canonical weigh-in per user-local day (upsert on chat_id+date,
  /// spec §7.1). [source] is 'nl' for executor writes, 'manual' for the
  /// Body page.
  Future<void> saveBodyWeight(String date, double kg, {String source});

  /// Weigh-ins with [startDate] <= date <= [endDate], ascending — the
  /// same window shape as the server's get_body_weights.
  Future<List<WeightEntry>> listBodyWeights(String startDate, String endDate);
  Future<void> deleteBodyWeight(String date);

  /// App-only (spec §9): full-row upsert of the day's girth measurements.
  /// Null means "no value for this metric that day" and OVERWRITES — the
  /// Body page's sheet is prefilled with the existing row, so unchanged
  /// fields carry through and a cleared field really clears.
  Future<void> saveBodyMeasurements(String date,
      {double? waistCm, double? chestCm, double? hipCm});
  Future<List<BodyMeasurements>> listBodyMeasurements(
      String startDate, String endDate);
  Future<void> deleteBodyMeasurements(String date);
  Future<void> saveActivity(String date,
      {num? activeCalories, int? steps, double? distanceKm});
  Future<String> exportJson(); // full data export (spec §8)

  /// Merge a previously exported envelope into THIS database (§9 app-only).
  /// Never destructive: existing rows win, new rows are added, and the
  /// result reports what happened. Throws [FormatException] on a payload
  /// that is not a CalorieTracker export.
  Future<ImportSummary> importJson(String json);
}

/// One weigh-in row (body_weight): a date and the kilograms recorded for it.
class WeightEntry {
  const WeightEntry(this.date, this.kg, {this.source = 'manual'});
  final String date; // YYYY-MM-DD
  final double kg;
  final String source;
}

/// One day's girth measurements (body_measurements, app-only spec §9).
/// Any subset of the three may be present.
class BodyMeasurements {
  const BodyMeasurements(this.date, {this.waistCm, this.chestCm, this.hipCm});
  final String date; // YYYY-MM-DD
  final double? waistCm;
  final double? chestCm;
  final double? hipCm;

  bool get isEmpty => waistCm == null && chestCm == null && hipCm == null;
}

/// Outcome of [MealsDao.importJson] — per-table added/skipped counts so the
/// UI can say "142 meals added, 3 already here" instead of "done".
class ImportSummary {
  const ImportSummary(this.added, this.skipped, {this.exportedAt});
  final Map<String, int> added;
  final Map<String, int> skipped;
  final String? exportedAt;

  int get totalAdded => added.values.fold(0, (a, b) => a + b);
  int get totalSkipped => skipped.values.fold(0, (a, b) => a + b);
}

/// Result of one photo analysis (spec §3).
class AnalysisOutcome {
  final Map<String, dynamic>? analysis; // null = failed (kept for retry)
  final bool isFood; // already coerced via coerceIsFood
  final String? error; // user-facing failure summary

  /// True when the failure is transient (rate limit, network, quota pause,
  /// missing key) — the photo itself is fine and a later attempt can
  /// succeed. Automated intake RELEASES the reservation instead of burning
  /// it to 'failed' (which the watcher never reclaims, spec §2.3): on
  /// EMUI-class OSes mid-analysis kills and quota pauses are routine, and
  /// burning on them silently drops the user's photos forever.
  final bool retryable;
  final Duration wall;
  const AnalysisOutcome(
      {this.analysis,
      this.isFood = false,
      this.error,
      this.retryable = false,
      required this.wall});
}

/// The analysis seam (spec §3). FOUR implementations satisfy it — Gemini
/// REST, OpenAI, Anthropic, and the user's own server running Claude Code —
/// so the contract is deliberately transport-agnostic: callers hand ORIGINAL
/// bytes; the implementation normalizes (1568px q85 JPEG), asks its provider
/// for JSON, applies the §3.3 retry/quota-pause rules, and returns COERCED
/// output. Never throws for model/network trouble.
abstract class AnalyzerService {
  /// [recentMeals]: compact same-day meal summaries (leftover_logic
  /// .recentMealCompact shape) for the AUTOMATIC leftover check
  /// (2026-08-05) — direct providers format them into the prompt's
  /// RECENT MEALS block; the server provider ships them as data and the
  /// server composes its own block. Null/empty = no check.
  Future<AnalysisOutcome> analyzePhoto(Uint8List originalBytes,
      {List<Map<String, dynamic>>? recentMeals});
  Future<Map<String, dynamic>?> textIntent(String prompt); // raw JSON or null
  Future<String?> validateKey(String apiKey); // null = OK, else error text

  /// Leftover-photo estimation (spec §9, 2026-08-04): given the leftover
  /// photo and the COMPACT original analysis, return the model's
  /// {same_meal, leftover_fraction, items, note} JSON, or null when this
  /// provider cannot estimate. Default null keeps existing fakes and
  /// third-party implementations compiling; real providers override.
  Future<Map<String, dynamic>?> leftoverIntent(
          Uint8List originalBytes, String originalCompact) async =>
      null;

  /// Richer probe for the diagnostics page. [validateKey] deliberately
  /// ACCEPTS a quota-class reply (it proves the key authenticated), which
  /// makes "key fine, account empty" indistinguishable from "all good" —
  /// so diagnostics could never name the out-of-credit case it promises
  /// to name. Default: derive from validateKey so third-party/fake
  /// implementations keep working.
  Future<KeyProbe> probeKey(String apiKey) async {
    final error = await validateKey(apiKey);
    return error == null
        ? const KeyProbe(KeyProbeResult.ok)
        : KeyProbe(KeyProbeResult.rejected, message: error);
  }
}

/// What a [AnalyzerService.probeKey] learned about the credential.
enum KeyProbeResult {
  ok,

  /// The key authenticated but the account cannot pay — a recharge (or a
  /// free-tier provider) is the fix, NOT a new key.
  outOfCredit,

  /// The key authenticated but the provider is rate-limiting right now.
  rateLimited,

  /// The provider refused the credential, or the request could not be made.
  rejected,
}

class KeyProbe {
  const KeyProbe(this.result, {this.message});
  final KeyProbeResult result;
  final String? message;
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

/// Result of describing a meal in free text: [analysis] on success (the
/// coerced food analysis, NOT yet saved), else [error] for display.
class DescribeOutcome {
  const DescribeOutcome({this.analysis, this.error, this.warning});
  final Map<String, dynamic>? analysis;
  final String? error;

  /// Non-fatal honesty: set when the reply described MORE meals than the
  /// single previewed one. Silently keeping the first would under-count the
  /// user's day with no way to notice.
  final String? warning;
  bool get ok => analysis != null;
}

/// The hardened NL executor (spec §4): builds the prompt from the meals
/// snapshot, normalizes single/multi/bare-array responses, caps at 5
/// actions, merges duplicate deletes, contains per-action failures, and
/// NEVER deletes without confirmation.
abstract class NlExecutor {
  Future<List<NlReply>> handleText(String userText);
  Future<String> confirmPendingDelete(List<int> mealIds);

  /// Analyze free text as a NEW meal ONLY — never a correction or delete —
  /// and return it for PREVIEW without saving. Works in any language (the
  /// shared prompt is multilingual). The dedicated entry exists because
  /// handleText is intent-routed: "a latte and two eggs" against a
  /// non-empty recent-meals list can legitimately parse as a correction of
  /// an existing meal, which is not what someone typing into a "describe a
  /// meal" box means.
  Future<DescribeOutcome> describeMeal(String text);
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

  /// Backpressured delivery: when a sink is attached, the intake DELIVERS
  /// each photo by awaiting it — reading the next photo's bytes only after
  /// the sink returns — instead of adding to [photos]. Without this, a scan
  /// reads the whole window's original bytes (25 MB each) ahead of a
  /// pipeline that takes 10–20 s per photo: a 200-photo backlog ≈ 1 GB
  /// resident → OOM-killed background runs in a loop.
  ///
  /// [safeFrontier] is the watermark value that is safe to persist IF this
  /// photo terminates successfully: it is createDate-based, halts at any
  /// transient failure earlier in the batch, and is null for the whole
  /// scan when the window was truncated (unseen older tail). Consumers
  /// checkpoint EXACTLY this value — deriving a watermark from the photo
  /// itself (e.g. filename dates) diverges from the createDate timebase
  /// the watermark filters on and skips photos permanently.
  ///
  /// The sink returns false when the photo was RELEASED for retry
  /// (transient failure) — the intake then forgets it was seen so a later
  /// scan re-offers it. Pass null to detach.
  void attachSink(
      Future<bool> Function(IntakePhoto photo, DateTime? safeFrontier)? sink);

  /// Scan the lookback window and emit every photo in it. [since], when
  /// LATER than the window cutoff, narrows the scan to photos created after
  /// it — the periodic background job passes its persisted watermark so a
  /// 30-minute cadence doesn't re-read the whole window's bytes every run.
  /// Returns the coverage FRONTIER: the newest createDate with everything
  /// at/before it terminally handled (a retryable release or transient read
  /// failure halts it) — watermarks must never advance past it. Null =
  /// nothing covered.
  Future<DateTime?> backfillScan({int lookbackDays, DateTime? since});
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
