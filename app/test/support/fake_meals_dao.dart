/// ONE in-memory MealsDao for the whole suite.
///
/// Before this existed the 18-method contract was hand-faked three times
/// (test/ui/fakes.dart, test/nl/fakes.dart, test/report/builders_test.dart —
/// 368 lines), and two of the last four feature commits had to edit all
/// three in lockstep. Adding a contract method is now a one-file change.
///
/// Deliberate shape, from the design-review judging:
///   * NO `seed` here. The ui suite's `void seed(Meal)` and the nl suite's
///     `int seed(String, num, {...})` have incompatible signatures, so a
///     base declaration would make one of them an illegal override.
///   * [nextId] is public: subclasses allocate ids from the SAME counter, or
///     a subclass-local one would collide with ids handed out here.
///   * Every recorder lives here even if only one suite reads it — recorders
///     are inert, and centralizing them is what lets the subclasses shrink.
library;

import 'dart:convert';
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/data/meals_logic.dart';

class BaseFakeDao implements MealsDao {
  final List<Meal> meals = [];
  final Map<String, IngestionStatus> ledger = {};
  final Map<int, Uint8List> thumbs = {};

  /// Rows inserted via [saveMeal] only — lets a test tell an INSERT from an
  /// in-place edit (an editor save must never do both).
  final List<Meal> saved = [];

  /// Alias for suites that named the same list `savedMeals`.
  List<Meal> get savedMeals => saved;

  /// Editor saves, in order: (id, analysis, date, time).
  final List<(int, Map<String, dynamic>?, String?, String?)> fieldUpdates = [];

  /// NL-style recorders.
  final List<(int, Map<String, dynamic>)> updates = [];
  final List<int> deletedIds = [];
  final List<(String, double)> savedWeights = [];
  final List<Map<String, dynamic>> savedActivities = [];

  /// Knobs.
  bool duplicatePhoto = false;
  bool throwOnUpdate = false;
  int? lastRecentDays;

  /// Public so subclasses' own seed() helpers share the id sequence.
  int nextId = 1;

  Meal byId(int id) => meals.firstWhere((m) => m.id == id);

  /// Insert [meal] with a fresh id (or keep a non-zero one), returning it.
  int put(Meal meal) {
    final id = meal.id == 0 ? nextId++ : meal.id;
    meals.add(Meal(
      id: id,
      date: meal.date,
      time: meal.time,
      timestamp: meal.timestamp,
      source: meal.source,
      imageHash: meal.imageHash,
      fileId: meal.fileId,
      analysis: meal.analysis,
      corrected: meal.corrected,
    ));
    return id;
  }

  @override
  Future<int> saveMeal(Meal meal, {IngestionStatus? markStatus}) async {
    saved.add(meal);
    final id = put(Meal(
      id: 0,
      date: meal.date,
      time: meal.time,
      timestamp: meal.timestamp,
      source: meal.source,
      imageHash: meal.imageHash,
      fileId: meal.fileId,
      analysis: meal.analysis,
      corrected: meal.corrected,
    ));
    if (markStatus != null && meal.imageHash.isNotEmpty) {
      ledger[meal.imageHash] = markStatus;
    }
    return id;
  }

  @override
  Future<void> updateMealAnalysis(
      int mealId, Map<String, dynamic> analysis) async {
    if (throwOnUpdate) throw StateError('boom');
    updates.add((mealId, analysis));
    _patch(mealId, analysis: analysis);
  }

  @override
  Future<void> updateMealFields(int mealId,
      {Map<String, dynamic>? analysis, String? date, String? time}) async {
    if (throwOnUpdate) throw StateError('boom');
    fieldUpdates.add((mealId, analysis, date, time));
    if (analysis != null) updates.add((mealId, analysis));
    _patch(mealId, analysis: analysis, date: date, time: time);
  }

  void _patch(int mealId,
      {Map<String, dynamic>? analysis, String? date, String? time}) {
    final i = meals.indexWhere((m) => m.id == mealId);
    if (i < 0) return;
    final m = meals[i];
    meals[i] = Meal(
      id: m.id,
      date: date ?? m.date,
      time: time ?? m.time,
      timestamp: m.timestamp, // ingestion time is never moved (spec §2.2)
      source: m.source,
      imageHash: m.imageHash,
      fileId: m.fileId,
      analysis: analysis ?? m.analysis,
      corrected: true, // any correction write sets corrected=1 (spec §2.4)
    );
  }

  @override
  Future<void> deleteMeal(int mealId) async {
    deletedIds.add(mealId);
    meals.removeWhere((m) => m.id == mealId);
    thumbs.remove(mealId);
  }

  @override
  Future<List<Meal>> mealsBetween(String startDate, String endDate) async =>
      meals
          .where((m) =>
              m.date.compareTo(startDate) >= 0 &&
              m.date.compareTo(endDate) <= 0)
          .toList();

  @override
  Future<List<Meal>> recentMeals({int days = 7}) async {
    lastRecentDays = days;
    return List.of(meals);
  }

  @override
  Future<bool> isDuplicatePhoto(String imageHash) async => duplicatePhoto;

  @override
  Future<bool> reservePhotoHash(String imageHash,
      {required String source, bool reclaimDeliberate = false}) async {
    final existing = ledger[imageHash];
    if (existing == null) {
      ledger[imageHash] = IngestionStatus.processing;
      return true;
    }
    if (reclaimDeliberate &&
        const {
          IngestionStatus.failed,
          IngestionStatus.skipped,
          IngestionStatus.deleted
        }.contains(existing)) {
      ledger[imageHash] = IngestionStatus.processing;
      return true;
    }
    return false;
  }

  @override
  Future<void> markPhotoHash(String imageHash, IngestionStatus status,
      {int? mealId}) async {
    ledger[imageHash] = status;
  }

  @override
  Future<void> releasePhotoHash(String imageHash) async {
    if (ledger[imageHash] == IngestionStatus.processing) {
      ledger.remove(imageHash);
    }
  }

  @override
  Future<void> reclaimStaleProcessing() async {}

  @override
  Future<({IngestionStatus status, int? mealId})?> photoStatus(
      String imageHash) async {
    final st = ledger[imageHash];
    if (st == null) return null;
    final match = meals.where((m) => m.imageHash == imageHash).toList();
    return (status: st, mealId: match.isEmpty ? null : match.first.id);
  }

  @override
  Future<void> saveMealThumb(int mealId, Uint8List jpeg) async =>
      thumbs[mealId] = jpeg;

  @override
  Future<Uint8List?> mealThumb(int mealId) async => thumbs[mealId];

  @override
  Future<void> saveBodyWeight(String date, double kg) async {
    savedWeights.add((date, kg));
  }

  @override
  Future<void> saveActivity(String date,
      {num? activeCalories, int? steps, double? distanceKm}) async {
    savedActivities.add({
      'date': date,
      'active_calories': activeCalories,
      'steps': steps,
      'distance_km': distanceKm,
    });
  }

  /// The REAL envelope shape (meals_logic.buildExportEnvelope) — a flat map
  /// here once made the report reader's top-level lookup look correct while
  /// production returned nothing.
  @override
  Future<String> exportJson() async => jsonEncode({
        'format': 'calorie_tracker_export',
        'version': 1,
        'exported_at': '2026-07-17T20:00:00.000',
        'tables': const {'meals': <Object>[]},
      });

  /// Validates like production (a wrong file must throw here too) and
  /// records the payload; row merging is the real DAO's job, pinned by the
  /// conformance suite against real SQLite.
  final List<String> imported = [];

  @override
  Future<ImportSummary> importJson(String json) async {
    final parsed = parseExportEnvelope(json);
    imported.add(json);
    return ImportSummary(
      {for (final e in parsed.tables.entries) e.key: e.value.length},
      const {},
      exportedAt: parsed.exportedAt,
    );
  }
}
