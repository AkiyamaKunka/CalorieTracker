import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// In-memory stand-in for flutter_secure_storage (no platform channel in
/// unit tests).
class MemoryKeyStore implements SecureKeyStore {
  final Map<String, String> data = {};
  @override
  Future<String?> read(String key) async => data[key];
  @override
  Future<void> write(String key, String value) async => data[key] = value;
  @override
  Future<void> delete(String key) async => data.remove(key);
}

Future<(AppSettings, SharedPreferences, MemoryKeyStore)> freshSettings(
    {Map<String, Object> initial = const {}}) async {
  SharedPreferences.setMockInitialValues(initial);
  final prefs = await SharedPreferences.getInstance();
  final keys = MemoryKeyStore();
  final s = await AppSettings.load(prefs: prefs, keyStore: keys);
  return (s, prefs, keys);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('defaults on a fresh install', () async {
    final (s, _, _) = await freshSettings();
    expect(s.geminiApiKey, isNull);
    expect(s.model, 'gemini-2.5-flash');
    expect(s.lookbackDays, 2);
    expect(s.reportTime, '21:30');
    expect(s.watcherEnabled, isFalse);
    expect(s.dietaryProfile, isNull);
    expect(s.quotaPauseUntil, isNull);
    expect(s.isQuotaPaused, isFalse);
  });

  test('setters persist and a second load sees the values', () async {
    final (s, prefs, keys) = await freshSettings();
    await s.setGeminiApiKey('  sk-test-123  ');
    await s.setModel('gemini-2.5-pro');
    await s.setLookbackDays(7);
    await s.setReportTime('08:05');
    await s.setWatcherEnabled(true);
    await s.setDietaryProfile('vegetarian, Cantonese cuisine');
    final pause = DateTime(2026, 7, 18, 9, 30);
    await s.setQuotaPauseUntil(pause);

    final again = await AppSettings.load(prefs: prefs, keyStore: keys);
    expect(again.geminiApiKey, 'sk-test-123');
    expect(again.model, 'gemini-2.5-pro');
    expect(again.lookbackDays, 7);
    expect(again.reportTime, '08:05');
    expect(again.watcherEnabled, isTrue);
    expect(again.dietaryProfile, 'vegetarian, Cantonese cuisine');
    expect(again.quotaPauseUntil, pause);
  });

  test('CHANGING the key clears the quota-pause latch; re-saving it does not',
      () async {
    final (s, prefs, keys) = await freshSettings();
    await s.setGeminiApiKey('key-A');
    final pause = DateTime.now().add(const Duration(hours: 6));
    await s.setQuotaPauseUntil(pause);

    // Same key re-entered: the pause still belongs to it — keep the latch.
    await s.setGeminiApiKey('key-A');
    expect(s.quotaPauseUntil, pause);

    // Different key: fresh project, fresh quota — a stale latch here means
    // "Key OK" with silently dead analysis for up to 12 h.
    await s.setGeminiApiKey('key-B');
    expect(s.quotaPauseUntil, isNull);
    final again = await AppSettings.load(prefs: prefs, keyStore: keys);
    expect(again.quotaPauseUntil, isNull); // cleared in persistence too
  });

  test('API key lives only in secure storage, never in prefs', () async {
    final (s, prefs, keys) = await freshSettings();
    await s.setGeminiApiKey('sk-secret');
    expect(keys.data.values, contains('sk-secret'));
    for (final k in prefs.getKeys()) {
      expect(prefs.get(k).toString(), isNot(contains('sk-secret')));
    }
  });

  test('clearing the API key deletes it from secure storage', () async {
    final (s, _, keys) = await freshSettings();
    await s.setGeminiApiKey('sk-secret');
    await s.setGeminiApiKey('   ');
    expect(s.geminiApiKey, isNull);
    expect(keys.data, isEmpty);
  });

  test('lookbackDays clamps to 1..30 on set and on load', () async {
    final (s, prefs, keys) = await freshSettings();
    await s.setLookbackDays(0);
    expect(s.lookbackDays, 1);
    await s.setLookbackDays(99);
    expect(s.lookbackDays, 30);
    // A hand-edited out-of-range stored value clamps at load time too.
    await prefs.setInt('settings.lookback_days', 500);
    final again = await AppSettings.load(prefs: prefs, keyStore: keys);
    expect(again.lookbackDays, 30);
  });

  test('reportTime rejects non-HH:mm values and survives bad stored data',
      () async {
    final (s, prefs, keys) = await freshSettings();
    expect(() => s.setReportTime('9pm'), throwsArgumentError);
    expect(() => s.setReportTime('25:00'), throwsArgumentError);
    expect(s.reportTime, '21:30');
    await prefs.setString('settings.report_time', 'garbage');
    final again = await AppSettings.load(prefs: prefs, keyStore: keys);
    expect(again.reportTime, '21:30');
  });

  test('blank model resets to the default', () async {
    final (s, _, _) = await freshSettings();
    await s.setModel('   ');
    expect(s.model, 'gemini-2.5-flash');
  });

  test('blank dietary profile reads back as null', () async {
    final (s, prefs, keys) = await freshSettings();
    await s.setDietaryProfile('  ');
    expect(s.dietaryProfile, isNull);
    final again = await AppSettings.load(prefs: prefs, keyStore: keys);
    expect(again.dietaryProfile, isNull);
  });

  test('isQuotaPaused is true only while now < pause-until', () async {
    final (s, _, _) = await freshSettings();
    await s.setQuotaPauseUntil(DateTime.now().add(const Duration(hours: 1)));
    expect(s.isQuotaPaused, isTrue);
    await s.setQuotaPauseUntil(
        DateTime.now().subtract(const Duration(seconds: 1)));
    expect(s.isQuotaPaused, isFalse);
    await s.setQuotaPauseUntil(null);
    expect(s.quotaPauseUntil, isNull);
  });

  test('setters notify listeners', () async {
    final (s, _, _) = await freshSettings();
    var notifications = 0;
    s.addListener(() => notifications++);
    await s.setModel('m');
    await s.setLookbackDays(3);
    await s.setWatcherEnabled(true);
    await s.setGeminiApiKey('k');
    expect(notifications, 4);
  });
}
