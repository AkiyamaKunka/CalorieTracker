/// In-memory fakes for the core seams (spec test rules: no network, no disk).
library;

import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../support/fake_meals_dao.dart';

/// The nl suite's DAO: [BaseFakeDao] plus the description-based `seed()`
/// these tests use to build a meals list quickly. Recorders (updates,
/// deletedIds, savedWeights, savedActivities, throwOnUpdate, lastRecentDays)
/// all live in the base.
class FakeDao extends BaseFakeDao {
  int seed(String desc, num cal,
      {String date = '2026-07-17',
      String time = '12:00 PM',
      Map<String, dynamic>? analysis}) {
    return put(Meal(
      id: 0,
      date: date,
      time: time,
      timestamp: DateTime.now().toIso8601String(),
      source: 'test',
      analysis: analysis ??
          {'is_food': true, 'meal_description': desc, 'total_calories': cal},
    ));
  }
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
  final s = await AppSettings.load(
      prefs: await SharedPreferences.getInstance(), keyStore: MemoryKeyStore());
  // A key must exist: the executor now refuses keyless requests with an
  // actionable message BEFORE calling the analyzer, and these suites test
  // the post-key behavior. test/nl/missing_key_test.dart owns the
  // keyless path.
  await s.setGeminiApiKey('test-key');
  return s;
}
