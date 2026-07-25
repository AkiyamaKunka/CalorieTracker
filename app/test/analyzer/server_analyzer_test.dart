// The "my server (Claude subscription)" provider: the app POSTs to the
// user's own VM, which runs the Claude Code CLI under their subscription.
// What must hold: the right endpoint per call shape, the upload key in the
// header, the app's dietary-profile prompt travelling with the request, and
// server 503 ("CLI busy/unavailable") classified RETRYABLE so the photo
// stays eligible instead of being burned.
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

Future<AppSettings> serverSettings({
  String? url = 'http://10.0.0.5',
  String? key = 'upload-key',
  String profile = '',
}) async {
  SharedPreferences.setMockInitialValues({});
  final s = await AppSettings.load(
      prefs: await SharedPreferences.getInstance(),
      keyStore: MemoryKeyStore());
  await s.setProvider(AiProvider.server);
  if (url != null) await s.setServerBaseUrl(url);
  if (key != null) await s.setServerApiKey(key);
  if (profile.isNotEmpty) await s.setDietaryProfile(profile);
  return s;
}

Uint8List jpeg() => Uint8List.fromList([1, 2, 3, 4]);

void main() {
  test('photo call sends the PROFILE, never a caller-authored prompt',
      () async {
    final s = await serverSettings(profile: 'Cantonese home cooking');
    final requests = <http.Request>[];
    final analyzer = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      client: MockClient((req) async {
        requests.add(req);
        return http.Response(
            jsonEncode({
              'ok': true,
              'analysis': {
                'is_food': true,
                'total_calories': 610,
                'meal_description': 'Steamed fish'
              },
              'analyzed_by': 'claude',
            }),
            200);
      }),
    );

    final outcome = await analyzer.analyzePhoto(jpeg());
    expect(outcome.isFood, isTrue);
    expect(outcome.analysis!['total_calories'], 610);

    final req = requests.single;
    expect(req.url.toString(), 'http://10.0.0.5/api/analyze_photo');
    expect(req.headers['X-API-Key'], 'upload-key');
    final body = jsonDecode(req.body) as Map<String, dynamic>;
    expect(base64Decode(body['image_b64'] as String), [1, 2, 3, 4]);
    // Only the dietary PROFILE crosses the wire; the server composes the
    // prompt from its own shared/ copy. A caller-authored prompt would be
    // an instruction channel into a CLI whose image path enables Read.
    expect(body['dietary_profile'], 'Cantonese home cooking');
    expect(body.containsKey('prompt'), isFalse);
  });

  test('text intent hits /api/text_intent and unwraps result', () async {
    final s = await serverSettings();
    final requests = <http.Request>[];
    final analyzer = ServerAnalyzer(s, client: MockClient((req) async {
      requests.add(req);
      return http.Response(
          jsonEncode({
            'ok': true,
            'result': {'intent': 'correction', 'meal_index': 1},
          }),
          200);
    }));

    final out = await analyzer.textIntent('make lunch 600 kcal');
    expect(out, {'intent': 'correction', 'meal_index': 1});
    expect(requests.single.url.path, '/api/text_intent');
    expect(jsonDecode(requests.single.body)['prompt'], 'make lunch 600 kcal');
    expect(jsonDecode(requests.single.body).containsKey('image_b64'), isFalse);
  });

  test('bare-array text replies still normalize (§4.1 parity)', () async {
    final s = await serverSettings();
    final analyzer = ServerAnalyzer(s, client: MockClient((_) async {
      return http.Response(
          jsonEncode({
            'ok': true,
            'result': [
              {'intent': 'correction'},
              {'intent': 'delete_meal'},
            ],
          }),
          200);
    }));
    final out = await analyzer.textIntent('two things');
    expect(out!['actions'], hasLength(2));
  });

  test('503 (CLI busy / not configured) is RETRYABLE, not a burned photo',
      () async {
    final s = await serverSettings();
    final sleeps = <Duration>[];
    final analyzer = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      sleep: (d) async => sleeps.add(d),
      client: MockClient((_) async => http.Response(
          jsonEncode({'error': 'claude_unavailable'}), 503)),
    );
    final outcome = await analyzer.analyzePhoto(jpeg());
    expect(outcome.analysis, isNull);
    expect(outcome.retryable, isTrue,
        reason: 'a busy subscription CLI must not mark the photo failed');
    expect(sleeps, hasLength(2)); // bounded in-place retries, then give up
  });

  test('401 from the server is a permanent key error', () async {
    final s = await serverSettings();
    final analyzer = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      client: MockClient((_) async =>
          http.Response(jsonEncode({'error': 'Unauthorized'}), 401)),
    );
    final outcome = await analyzer.analyzePhoto(jpeg());
    expect(outcome.retryable, isFalse);
    expect(outcome.error, contains('key'));
  });

  test('a missing server address is reported, never a bogus relative URL',
      () async {
    final s = await serverSettings(url: null);
    var calls = 0;
    final analyzer = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      client: MockClient((_) async {
        calls++;
        return http.Response('{}', 200);
      }),
    );
    final outcome = await analyzer.analyzePhoto(jpeg());
    expect(calls, 0);
    expect(outcome.error, contains('server address'));
    expect(outcome.retryable, isTrue); // the user can still add it
    // A URL-less server provider must read as "no key" to the guards.
    expect(s.activeApiKey, isNull);
  });

  test('validateKey uses /api/auth_check (never /ping) and maps outcomes',
      () async {
    final s = await serverSettings();
    final seen = <http.BaseRequest>[];
    Future<String?> validate(int status) {
      final analyzer = ServerAnalyzer(s, client: MockClient((req) async {
        seen.add(req);
        return http.Response('{}', status);
      }));
      return analyzer.validateKey('upload-key');
    }

    expect(await validate(200), isNull);
    // NOT /ping: that endpoint stamps the Termux watcher's heartbeat, so
    // testing the connection would forge watcher liveness and mute the
    // stale-watcher outage alert.
    expect(seen.single.url.path, '/api/auth_check');
    expect(seen.map((r) => r.url.path), isNot(contains('/ping')));
    expect(seen.single.headers['X-API-Key'], 'upload-key');
    expect(await validate(401), contains('rejected'));
    expect(await validate(500), contains('500'));
  });

  test('validateKey refuses before an address is set', () async {
    final s = await serverSettings(url: null);
    final analyzer = ServerAnalyzer(s, client: MockClient((_) async {
      fail('must not call the network without an address');
    }));
    expect(await analyzer.validateKey('k'), contains('server address'));
  });

  test('server provider allows far longer than the 90s API deadline',
      () async {
    final s = await serverSettings();
    final analyzer = ServerAnalyzer(s, client: MockClient((_) async =>
        http.Response('{}', 200)));
    // The server runs a Claude CLI analysis behind a synchronous handler;
    // giving up at 90 s abandons work that is still running and the retry
    // only meets the server's own single-flight lock.
    expect(analyzer.deadline.inSeconds, greaterThanOrEqualTo(240));
  });

  test('MultiProviderAnalyzer routes to the server when selected', () async {
    final s = await serverSettings();
    final paths = <String>[];
    final analyzer = createMultiProviderAnalyzer(s,
        client: MockClient((req) async {
      paths.add(req.url.path);
      return http.Response(
          jsonEncode({'ok': true, 'result': {'intent': 'query'}}), 200);
    }));
    await analyzer.textIntent('what did I eat');
    expect(paths, ['/api/text_intent']);
  });
}
