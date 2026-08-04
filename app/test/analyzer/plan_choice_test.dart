// Claude-plan model/effort choice (2026-08-05): the request bodies carry
// the user's pick ONLY on the claude backend ('' = omit = server
// default) — the vendor plans map models server-side and the API 400s
// overrides for them, so leaking a choice there breaks every photo.
import 'dart:convert';
import 'dart:typed_data';

import 'package:calorie_tracker/services/analyzer/provider_analyzers.dart';
import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

class MemoryKeyStore implements SecureKeyStore {
  final Map<String, String> data = {};
  @override
  Future<String?> read(String key) async => data[key];
  @override
  Future<void> write(String key, String value) async => data[key] = value;
  @override
  Future<void> delete(String key) async => data.remove(key);
}

Future<AppSettings> settingsWith(
    {String backend = 'claude', String model = '', String effort = ''}) async {
  SharedPreferences.setMockInitialValues({});
  final s = await AppSettings.load(
      prefs: await SharedPreferences.getInstance(),
      keyStore: MemoryKeyStore());
  await s.setProvider(AiProvider.server);
  await s.setServerBaseUrl('http://s.example');
  await s.setServerApiKey('upload-key');
  await s.setServerBackend(backend);
  await s.setServerModel(model);
  await s.setServerEffort(effort);
  return s;
}

Uint8List jpeg() => Uint8List.fromList([1, 2, 3, 4]);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  Future<Map<String, dynamic>> photoBody(AppSettings s) async {
    final requests = <http.Request>[];
    final analyzer = ServerAnalyzer(s,
        normalizer: (b) async => b,
        client: MockClient((req) async {
          requests.add(req);
          return http.Response(
              jsonEncode({
                'ok': true,
                'analysis': {'is_food': true, 'total_calories': 1},
                'analyzed_by': s.serverBackend,
              }),
              200);
        }));
    await analyzer.analyzePhoto(jpeg());
    return jsonDecode(requests.single.body) as Map<String, dynamic>;
  }

  test('claude backend carries the chosen model and effort', () async {
    final body =
        await photoBody(await settingsWith(model: 'haiku', effort: 'low'));
    expect(body['model'], 'haiku');
    expect(body['effort'], 'low');
  });

  test('server default omits the keys entirely', () async {
    final body = await photoBody(await settingsWith());
    expect(body.containsKey('model'), isFalse);
    expect(body.containsKey('effort'), isFalse);
  });

  test('a vendor plan NEVER carries the choice, even when stored',
      () async {
    final body = await photoBody(
        await settingsWith(backend: 'glm', model: 'opus', effort: 'high'));
    expect(body.containsKey('model'), isFalse,
        reason: 'the API 400s model overrides for vendor plans');
    expect(body.containsKey('effort'), isFalse);
  });

  test('the settings whitelist rejects junk values', () async {
    final s = await settingsWith();
    await s.setServerModel('--dangerously-anything');
    await s.setServerEffort('9000');
    expect(s.serverModel, '');
    expect(s.serverEffort, '');
    await s.setServerModel('sonnet');
    expect(s.serverModel, 'sonnet');
  });
}
