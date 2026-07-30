// A missing key used to surface as '❌ Error contacting AI. Please try
// again.' on the text paths — advice that can never work: the analyzer
// seam folds its precise missing-key exception into null. The executor now
// refuses BEFORE any model call, naming the provider and the fix.
import 'package:calorie_tracker/services/nl/executor.dart';
import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'fakes.dart';

Future<AppSettings> _keyless() async {
  SharedPreferences.setMockInitialValues(const {});
  return AppSettings.load(
      prefs: await SharedPreferences.getInstance(), keyStore: MemoryKeyStore());
}

void main() {
  test('chat with no key names the provider and Settings, no model call',
      () async {
    final s = await _keyless();
    await s.setProvider(AiProvider.qwen);
    final analyzer = FakeAnalyzer();
    final exec = DefaultNlExecutor(FakeDao(), analyzer, s);
    final replies = await exec.handleText('log a latte');
    expect(replies.single.text, contains('Qwen'));
    expect(replies.single.text, contains('Settings'));
    expect(replies.single.text, isNot(contains('try again')),
        reason: 'retrying can never fix a missing key');
    expect(analyzer.lastPrompt, isNull, reason: 'no model call, no spend');
  });

  test('describe with no key gets the same actionable refusal', () async {
    final s = await _keyless();
    final analyzer = FakeAnalyzer();
    final exec = DefaultNlExecutor(FakeDao(), analyzer, s);
    final out = await exec.describeMeal('two eggs');
    expect(out.ok, isFalse);
    expect(out.error, contains('Gemini'));
    expect(out.error, contains('Settings'));
    expect(analyzer.lastPrompt, isNull);
  });

  test('server provider distinguishes missing address from missing key',
      () async {
    final s = await _keyless();
    await s.setProvider(AiProvider.server); // no URL, no key
    final exec = DefaultNlExecutor(FakeDao(), FakeAnalyzer(), s);
    final replies = await exec.handleText('fix meal 1');
    expect(replies.single.text, contains('server address'));
  });

  test('with a key present the guard stays out of the way', () async {
    final s = await _keyless();
    await s.setGeminiApiKey('k');
    final analyzer = FakeAnalyzer()
      ..next = {'intent': 'unknown', 'reply': 'ok'};
    final exec = DefaultNlExecutor(FakeDao(), analyzer, s);
    await exec.handleText('hello');
    expect(analyzer.lastPrompt, isNotNull);
  });
}
