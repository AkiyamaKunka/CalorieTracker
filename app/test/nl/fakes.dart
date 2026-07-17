/// In-memory fakes for the core seams (spec test rules: no network, no disk).
library;

import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:shared_preferences/shared_preferences.dart';

class FakeDao implements MealsDao {
  final List<Meal> meals = [];
  int _nextId = 1;

  final List<(int, Map<String, dynamic>)> updates = [];
  final List<int> deletedIds = [];
  final List<Meal> savedMeals = [];
  final List<(String, double)> savedWeights = [];
  final List<Map<String, dynamic>> savedActivities = [];
  bool throwOnUpdate = false;
  int? lastRecentDays;

  int seed(String desc, num cal,
      {String date = '2026-07-17',
      String time = '12:00 PM',
      Map<String, dynamic>? analysis}) {
    final id = _nextId++;
    meals.add(Meal(
      id: id,
      date: date,
      time: time,
      timestamp: DateTime.now().toIso8601String(),
      source: 'test',
      analysis:
          analysis ?? {'is_food': true, 'meal_description': desc, 'total_calories': cal},
    ));
    return id;
  }

  Meal byId(int id) => meals.firstWhere((m) => m.id == id);

  @override
  Future<List<Meal>> recentMeals({int days = 7}) async {
    lastRecentDays = days;
    return List.of(meals);
  }

  @override
  Future<int> saveMeal(Meal meal, {IngestionStatus? markStatus}) async {
    final id = _nextId++;
    savedMeals.add(meal);
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
  Future<void> updateMealAnalysis(int mealId, Map<String, dynamic> analysis) async {
    if (throwOnUpdate) throw StateError('boom');
    updates.add((mealId, analysis));
    final i = meals.indexWhere((m) => m.id == mealId);
    if (i >= 0) {
      final m = meals[i];
      meals[i] = Meal(
        id: m.id,
        date: m.date,
        time: m.time,
        timestamp: m.timestamp,
        source: m.source,
        imageHash: m.imageHash,
        fileId: m.fileId,
        analysis: analysis,
        corrected: true, // spec §2.4: any correction write sets corrected=1
      );
    }
  }

  @override
  Future<void> deleteMeal(int mealId) async {
    deletedIds.add(mealId);
    meals.removeWhere((m) => m.id == mealId);
  }

  @override
  Future<List<Meal>> mealsBetween(String startDate, String endDate) async =>
      List.of(meals);

  @override
  Future<bool> isDuplicatePhoto(String imageHash) async => false;

  @override
  Future<bool> reservePhotoHash(String imageHash,
          {required String source, bool reclaimDeliberate = false}) async =>
      true;

  @override
  Future<void> markPhotoHash(String imageHash, IngestionStatus status,
      {int? mealId}) async {}

  @override
  Future<void> releasePhotoHash(String imageHash) async {}

  @override
  Future<void> reclaimStaleProcessing() async {}

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

  @override
  Future<String> exportJson() async => '{}';
}

class FakeAnalyzer implements AnalyzerService {
  Map<String, dynamic>? next;
  bool throwOnText = false;
  String? lastPrompt;

  @override
  Future<Map<String, dynamic>?> textIntent(String prompt) async {
    lastPrompt = prompt;
    if (throwOnText) throw StateError('network down');
    return next;
  }

  @override
  Future<AnalysisOutcome> analyzePhoto(Uint8List originalBytes) async =>
      const AnalysisOutcome(wall: Duration.zero);

  @override
  Future<String?> validateKey(String apiKey) async => null;
}

class MemoryKeyStore implements SecureKeyStore {
  final Map<String, String> data = {};
  @override
  Future<String?> read(String key) async => data[key];
  @override
  Future<void> write(String key, String value) async => data[key] = value;
  @override
  Future<void> delete(String key) async => data.remove(key);
}

Future<AppSettings> testSettings() async {
  SharedPreferences.setMockInitialValues(const {});
  return AppSettings.load(
      prefs: await SharedPreferences.getInstance(), keyStore: MemoryKeyStore());
}
