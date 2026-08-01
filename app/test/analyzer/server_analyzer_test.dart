// The "my server (Claude subscription)" provider: the app POSTs to the
// user's own VM, which runs the Claude Code CLI under their subscription.
// What must hold: the right endpoint per call shape, the upload key in the
// header, the app's dietary-profile prompt travelling with the request, and
// server 503 ("CLI busy/unavailable") classified RETRYABLE so the photo
// stays eligible instead of being burned.
import 'dart:convert';
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
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
    expect(body['backend'], 'claude',
        reason: 'the default backend travels explicitly');
  });

  test('the chosen server backend rides every request', () async {
    final s = await serverSettings();
    await s.setServerBackend('glm');
    final requests = <http.Request>[];
    final analyzer = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      client: MockClient((req) async {
        requests.add(req);
        return http.Response(
            jsonEncode({
              'ok': true,
              'analysis': {'is_food': true, 'total_calories': 1},
              'result': {'intent': 'x'},
            }),
            200);
      }),
    );
    await analyzer.analyzePhoto(jpeg());
    await analyzer.textIntent('hi');
    expect(requests, hasLength(2),
        reason: 'both call shapes must have been exercised');
    for (final req in requests) {
      expect(jsonDecode(req.body)['backend'], 'glm');
    }
  });

  test('serverBackend PERSISTS across a reload and rejects unknown values',
      () async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final store = MemoryKeyStore();
    final s = await AppSettings.load(prefs: prefs, keyStore: store);
    expect(s.serverBackend, 'claude');
    await s.setServerBackend('doubao');
    // A FRESH load from the same prefs must see the choice — the earlier
    // version of this test only re-read the in-memory field.
    final reloaded = await AppSettings.load(prefs: prefs, keyStore: store);
    expect(reloaded.serverBackend, 'doubao');
    await s.setServerBackend('gpt'); // unknown → safe default
    expect(s.serverBackend, 'claude');
  });

  test('validateKey surfaces a chosen backend that is not ready', () async {
    final s = await serverSettings();
    await s.setServerBackend('glm');
    final analyzer = ServerAnalyzer(s, client: MockClient((_) async {
      return http.Response(
          jsonEncode({
            'ok': true,
            'analyzer': 'enabled',
            'backends': {
              'claude': 'enabled',
              'glm': 'no key (GLM_PLAN_KEY)',
              'doubao': 'ready',
            },
          }),
          200);
    }));
    final msg = await analyzer.validateKey('upload-key');
    expect(msg, contains('GLM_PLAN_KEY'),
        reason: 'a missing server-side plan key must show at TEST time, '
            'not as a 503 on tonight’s dinner photo');

    await s.setServerBackend('doubao');
    expect(await analyzer.validateKey('upload-key'), isNull,
        reason: 'ready backend validates clean');

    await s.setServerBackend('claude');
    expect(await analyzer.validateKey('upload-key'), isNull);
  });

  test('validateKey WARNS about an old server when a vendor backend is '
      'chosen (it would silently bill the Claude plan)', () async {
    final s = await serverSettings();
    await s.setServerBackend('glm');
    final analyzer = ServerAnalyzer(s, client: MockClient((_) async {
      return http.Response(jsonEncode({'ok': true, 'analyzer': 'enabled'}), 200);
    }));
    expect(await analyzer.validateKey('upload-key'),
        contains('update the server'),
        reason: 'a real auth_check reply with no backends map IS the '
            'pre-upgrade server — the wrong payer must be said out loud');

    await s.setServerBackend('claude');
    expect(await analyzer.validateKey('upload-key'), isNull,
        reason: 'claude backend on an old server is exactly right');
  });

  test('validateKey does not cry "could not reach" over odd 200 bodies',
      () async {
    // A JSON array / non-map backends previously threw TypeError into the
    // generic catch → 'Could not reach …' about a server that ANSWERED.
    final s = await serverSettings();
    await s.setServerBackend('glm');
    for (final body in ['[]', '"ok"', '{"backends": "yes"}']) {
      final analyzer = ServerAnalyzer(s,
          client: MockClient((_) async => http.Response(body, 200)));
      final msg = await analyzer.validateKey('upload-key');
      expect(msg, isNot(contains('Could not reach')), reason: body);
    }
  });

  test('a 503 with a reason surfaces it VERBATIM — a refused backend must '
      'not look like a network hiccup', () async {
    // The 2026-07-31 incident: GLM selected, no plan key on the server —
    // every request 503ed and the app said only 'network or service
    // issue'. An hour of log archaeology later, this test.
    final s = await serverSettings();
    var calls = 0;
    final analyzer = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      sleep: (_) async {},
      client: MockClient((_) async {
        calls++;
        return http.Response(
            jsonEncode({
              'error': 'claude_unavailable',
              'reason': 'no key (GLM_PLAN_KEY)',
            }),
            503);
      }),
    );
    final out = await analyzer.analyzePhoto(jpeg());
    expect(out.retryable, isTrue, reason: 'config is fixable; keep photos');
    expect(calls, 1,
        reason: 'a 503 with no retry verdict must NOT spin — seconds '
            'cannot add a plan key');
    expect(out.error, contains('GLM_PLAN_KEY'),
        reason: 'the server said exactly what is missing — repeat it');
    // A bare 503 (busy CLI) keeps the generic transient message.
    final busy = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      client: MockClient((_) async =>
          http.Response('{"error": "claude_unavailable"}', 503)),
    );
    final busyOut = await busy.analyzePhoto(jpeg());
    expect(busyOut.retryable, isTrue);
    expect(busyOut.error, isNot(contains('GLM_PLAN_KEY')));
  });

  test('a BUSY 503 retries in place; an already-answered 503 does NOT',
      () async {
    // The server now says which kind of 503 this is. Retrying a run that
    // already happened spends another 120 s CLI run of the subscription
    // for the same answer (review 2026-07-31).
    final s = await serverSettings();
    var calls = 0;
    final terminal = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      sleep: (_) async {},
      client: MockClient((_) async {
        calls++;
        return http.Response(
            jsonEncode({
              'error': 'claude_unavailable',
              'reason': 'the analysis ran but produced no usable result',
              'retry': false,
            }),
            503);
      }),
    );
    final terminalOut = await terminal.analyzePhoto(jpeg());
    expect(calls, 1, reason: 'terminal 503 must not spend two more runs');
    expect(terminalOut.retryable, isFalse,
        reason: 'the run ALREADY answered — a watcher that keeps '
            're-offering this photo re-spends a full CLI run every scan');

    calls = 0;
    final busy = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      sleep: (_) async {},
      client: MockClient((_) async {
        calls++;
        return http.Response(
            jsonEncode({
              'error': 'claude_unavailable',
              'reason': 'the analyzer is busy with another photo',
              'retry': true,
            }),
            503);
      }),
    );
    final out = await busy.analyzePhoto(jpeg());
    expect(calls, 3, reason: 'a busy CLI is exactly what retries are for');
    expect(out.retryable, isTrue);
  });

  test('probeKey uses the FREE auth_check — never a CLI run — and keeps '
      'the readiness verdicts', () async {
    // The regression this pins (caught 2026-07-31, same day it shipped):
    // switching diagnostics from validateKey to probeKey made the SERVER
    // provider inherit the base probe, which posts a 'ping' prompt to
    // /api/text_intent — a full CLI run under the owner's subscription
    // whose prose reply then fails the JSON contract, so a healthy setup
    // was reported as "The key was not accepted" and the remaining
    // stages never ran.
    final s = await serverSettings();
    final paths = <String>[];
    ServerAnalyzer make(String body, int status) => ServerAnalyzer(s,
        normalizer: (b) async => b,
        client: MockClient((req) async {
          paths.add(req.url.path);
          return http.Response(body, status);
        }));

    final ok = await make(
            jsonEncode({'ok': true, 'backends': {'claude': 'enabled'}}), 200)
        .probeKey('upload-key');
    expect(paths, ['/api/auth_check'],
        reason: 'the probe must not spend a model call');
    expect(ok.result, KeyProbeResult.ok);

    // A server-side config fact is NOT a rejected key.
    paths.clear();
    await s.setServerBackend('glm');
    final notReady = await make(
        jsonEncode({
          'ok': true,
          'backends': {'glm': 'no key (GLM_PLAN_KEY)'},
        }),
        200).probeKey('upload-key');
    expect(notReady.result, isNot(KeyProbeResult.rejected),
        reason: 'the upload key WAS accepted; the plan key is missing');
    expect(notReady.message, contains('GLM_PLAN_KEY'));

    // A genuinely rejected key still rejects.
    paths.clear();
    final bad = await make('{"error": "Unauthorized"}', 401)
        .probeKey('upload-key');
    expect(bad.result, KeyProbeResult.rejected);
  });

  test('a reply analyzed by the WRONG backend is refused, not logged',
      () async {
    final s = await serverSettings();
    await s.setServerBackend('glm');
    final analyzer = ServerAnalyzer(
      s,
      normalizer: (b) async => b,
      client: MockClient((_) async => http.Response(
          jsonEncode({
            'ok': true,
            'analysis': {'is_food': true, 'total_calories': 500},
            'analyzed_by': 'claude', // pre-upgrade server: wrong payer
          }),
          200)),
    );
    final out = await analyzer.analyzePhoto(jpeg());
    expect(out.analysis, isNull,
        reason: 'accepting it would spend the Claude plan forever while '
            'the user believes GLM pays');
    expect(out.retryable, isFalse,
        reason: 'a retry spends the wrong plan again');
    expect(out.error, contains('update the server'));

    await s.setServerBackend('claude');
    final ok = await analyzer.analyzePhoto(jpeg());
    expect(ok.isFood, isTrue, reason: 'matching payer sails through');
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

  test('startClaudeAuth returns the official URL; completes with null on 200',
      () async {
    final s = await serverSettings();
    final calls = <http.Request>[];
    final analyzer = ServerAnalyzer(s, client: MockClient((req) async {
      calls.add(req);
      if (req.url.path == '/api/claude_auth/start') {
        return http.Response(
            jsonEncode({
              'ok': true,
              'url': 'https://claude.ai/oauth/authorize?code=true&x=1',
            }),
            200);
      }
      return http.Response(jsonEncode({'ok': true}), 200);
    }));

    final started = await analyzer.startClaudeAuth();
    expect(started.error, isNull);
    expect(started.url, startsWith('https://claude.ai/oauth/authorize'));
    expect(calls.single.headers['X-API-Key'], 'upload-key');

    expect(await analyzer.completeClaudeAuth('code#state'), isNull);
    expect(jsonDecode(calls.last.body)['code'], 'code#state');
    expect(calls.last.url.path, '/api/claude_auth/complete');
  });

  test('auth failures surface the server reason, never a crash', () async {
    final s = await serverSettings();
    final analyzer = ServerAnalyzer(s, client: MockClient((req) async {
      // charset header matters: Flask sends UTF-8, and http.Response's
      // default (latin-1) cannot encode the reason's em dash.
      return http.Response(
          jsonEncode({'error': 'complete_failed',
                      'reason': 'the sign-in expired — start again'}),
          502,
          headers: {'content-type': 'application/json; charset=utf-8'});
    }));
    final started = await analyzer.startClaudeAuth();
    expect(started.url, isNull);
    expect(started.error, contains('expired'));
    expect(await analyzer.completeClaudeAuth('x'), contains('expired'));
  });

  test('startClaudeAuth refuses without an address/key', () async {
    final s = await serverSettings(url: null);
    final analyzer = ServerAnalyzer(s, client: MockClient((_) async {
      fail('must not call the network unconfigured');
    }));
    final started = await analyzer.startClaudeAuth();
    expect(started.error, contains('address'));
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
