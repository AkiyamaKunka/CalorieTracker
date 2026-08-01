// _AppSettingsStore: the ONE piece of production code that maps the UI's
// provider-scoped apiKey/model onto AppSettings' seven per-provider slots.
//
// It had ZERO tests: every widget test used FakeSettings, which SIMULATES
// the routing correctly, so the suite would stay green while the real
// adapter wrote a Qwen key into the Gemini slot. The comment in
// widget_test.dart claimed this was "exercised at integration time" — but
// the E2E needs a live key and never runs in CI (review 2026-07-31).
import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:calorie_tracker/ui/di.dart';
import 'package:calorie_tracker/ui/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../analyzer/server_analyzer_test.dart' show MemoryKeyStore;

Future<(AppSettings, SettingsStore)> build() async {
  SharedPreferences.setMockInitialValues({});
  final settings = await AppSettings.load(
      prefs: await SharedPreferences.getInstance(),
      keyStore: MemoryKeyStore());
  return (settings, createSettingsStore(settings));
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('apiKey reads and writes the ACTIVE provider slot', () async {
    final (settings, store) = await build();
    await settings.setGeminiApiKey('gem');
    await settings.setQwenApiKey('qw');

    expect(store.apiKey, 'gem');
    await store.update(provider: 'qwen');
    expect(store.provider, 'qwen');
    expect(store.apiKey, 'qw', reason: 'the ACTIVE slot, not a cached one');

    // A write while Qwen is active must land in the QWEN slot only.
    await store.update(apiKey: 'qw2');
    expect(settings.qwenApiKey, 'qw2');
    expect(settings.geminiApiKey, 'gem',
        reason: "another provider's key must never be overwritten");
  });

  test('model is provider-scoped too, with each default preserved',
      () async {
    final (settings, store) = await build();
    expect(store.model, AppSettings.defaultModel);
    await store.update(provider: 'glm');
    expect(store.model, AppSettings.defaultGlmModel);
    await store.update(model: 'glm-4.6v');
    expect(settings.glmModel, 'glm-4.6v');

    await store.update(provider: 'doubao');
    expect(store.model, AppSettings.defaultDoubaoModel,
        reason: 'switching providers must not carry the GLM model over');
    expect(settings.glmModel, 'glm-4.6v', reason: 'and must not clobber it');
  });

  test('the server provider needs BOTH url and key to count as configured',
      () async {
    final (settings, store) = await build();
    await store.update(provider: 'server');
    expect(store.apiKey, isEmpty);
    await store.update(apiKey: 'upload-key');
    expect(store.apiKey, isEmpty,
        reason: 'a key without an address cannot analyze anything');
    await store.update(serverBaseUrl: 'http://10.0.0.5/');
    expect(store.apiKey, 'upload-key');
    expect(settings.serverBaseUrl, 'http://10.0.0.5',
        reason: 'trailing slashes are trimmed so path joins never double');
  });

  test('canAnalyze and the quota latch travel through the seam', () async {
    final (settings, store) = await build();
    expect(store.canAnalyze, isFalse, reason: 'no key yet');
    await store.update(apiKey: 'k');
    expect(store.canAnalyze, isTrue);

    await settings.setQuotaPauseUntil(
        DateTime.now().add(const Duration(hours: 2)));
    expect(store.isQuotaPaused, isTrue);
    expect(store.quotaPauseUntil, isNotNull);
    expect(store.canAnalyze, isFalse);
  });

  test('an unknown provider string is ignored, not crashed on', () async {
    final (_, store) = await build();
    await store.update(provider: 'not-a-provider');
    expect(store.provider, 'gemini');
  });

  test('serverBackend round-trips and rejects unknown values', () async {
    final (settings, store) = await build();
    expect(store.serverBackend, 'claude');
    await store.update(serverBackend: 'doubao');
    expect(store.serverBackend, 'doubao');
    expect(settings.serverBackend, 'doubao');
    await store.update(serverBackend: 'gpt');
    expect(store.serverBackend, 'claude');
  });
}
