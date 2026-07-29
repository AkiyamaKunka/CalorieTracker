// OpenAI + Anthropic backends and the per-call provider delegation.
// Mirrors gemini_analyzer_test's harness style: MockClient, injected
// settings via SharedPreferences mock + in-memory keystore, no platform
// channels, no live calls.
import 'dart:convert';
import 'dart:typed_data';

import 'package:calorie_tracker/services/analyzer/provider_analyzers.dart';
import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

class MemKeys implements SecureKeyStore {
  final Map<String, String> data = {};
  @override
  Future<String?> read(String key) async => data[key];
  @override
  Future<void> write(String key, String value) async => data[key] = value;
  @override
  Future<void> delete(String key) async => data.remove(key);
}

Future<AppSettings> settingsWith({
  String provider = 'openai',
  String? openaiKey = 'sk-oa',
  String? anthropicKey = 'sk-an',
  String? geminiKey,
  String? qwenKey,
  String? doubaoKey,
  String? glmKey,
}) async {
  SharedPreferences.setMockInitialValues({'settings.provider': provider});
  final prefs = await SharedPreferences.getInstance();
  final keys = MemKeys();
  if (openaiKey != null) keys.data['openai_api_key'] = openaiKey;
  if (anthropicKey != null) keys.data['anthropic_api_key'] = anthropicKey;
  if (geminiKey != null) keys.data['gemini_api_key'] = geminiKey;
  if (qwenKey != null) keys.data['qwen_api_key'] = qwenKey;
  if (doubaoKey != null) keys.data['doubao_api_key'] = doubaoKey;
  if (glmKey != null) keys.data['glm_api_key'] = glmKey;
  return AppSettings.load(prefs: prefs, keyStore: keys);
}

http.Response _openaiOk(String text) => http.Response(
    jsonEncode({
      'choices': [
        {
          'message': {'role': 'assistant', 'content': text}
        }
      ]
    }),
    200);

http.Response _anthropicOk(String text) => http.Response(
    jsonEncode({
      'content': [
        {'type': 'text', 'text': text}
      ]
    }),
    200);

const _foodJson =
    '{"is_food": true, "meal_description": "Noodles", "total_calories": 500,'
    ' "food_items": [{"name": "Noodles", "estimated_calories": 500}]}';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  final photo = Uint8List.fromList(List.filled(64, 7));

  group('OpenAiAnalyzer', () {
    test('vision request shape: bearer auth, data-URI image, json mode',
        () async {
      final s = await settingsWith();
      final requests = <http.Request>[];
      final a = OpenAiAnalyzer(s,
          client: MockClient((r) async {
            requests.add(r);
            return _openaiOk(_foodJson);
          }),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(out.isFood, isTrue);
      expect(out.analysis!['total_calories'], 500);
      final req = requests.single;
      expect(req.url.toString(),
          'https://api.openai.com/v1/chat/completions');
      expect(req.headers['Authorization'], 'Bearer sk-oa');
      final body = jsonDecode(req.body) as Map<String, dynamic>;
      expect(body['model'], 'gpt-4o-mini');
      expect(body['response_format'], {'type': 'json_object'});
      // max_tokens is REJECTED (400) by o-series/gpt-5 models; the newer
      // name is accepted across all current chat-completions models.
      expect(body.containsKey('max_tokens'), isFalse);
      expect(body['max_completion_tokens'], isA<int>());
      final content = ((body['messages'] as List).first
          as Map<String, dynamic>)['content'] as List;
      expect(
          (content[1] as Map)['image_url']['url'],
          startsWith('data:image/jpeg;base64,'));
    });

    test('429 exhausts retryless and reports RETRYABLE (release-not-burn)',
        () async {
      final s = await settingsWith();
      final a = OpenAiAnalyzer(s,
          client: MockClient((r) async => http.Response('{"error":{}}', 429)),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(out.analysis, isNull);
      expect(out.retryable, isTrue);
    });

    test('429 insufficient_quota surfaces a billing message, stays retryable',
        () async {
      final s = await settingsWith();
      final a = OpenAiAnalyzer(s,
          client: MockClient((r) async => http.Response(
              '{"error":{"type":"insufficient_quota","message":"..."}}', 429)),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(out.retryable, isTrue); // photo stays eligible
      expect(out.error, contains('billing'));
    });

    test('missing key: retryable outcome, NO in-place retry sleeps, clear '
        'message', () async {
      final s = await settingsWith(openaiKey: null);
      final sleeps = <Duration>[];
      var calls = 0;
      final a = OpenAiAnalyzer(s,
          client: MockClient((r) async {
            calls++;
            return _openaiOk(_foodJson);
          }),
          sleep: (d) async => sleeps.add(d),
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(calls, 0);
      expect(sleeps, isEmpty); // the condition can't change mid-call
      expect(out.retryable, isTrue);
      expect(out.error, contains('Settings'));
    });

    test('401 is permanent (auth), never retried', () async {
      final s = await settingsWith();
      var calls = 0;
      final a = OpenAiAnalyzer(s,
          client: MockClient((r) async {
            calls++;
            return http.Response('{"error":{}}', 401);
          }),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(out.retryable, isFalse);
      expect(calls, 1);
    });

    test('5xx retries then reports retryable', () async {
      final s = await settingsWith();
      var calls = 0;
      final a = OpenAiAnalyzer(s,
          client: MockClient((r) async {
            calls++;
            return http.Response('overloaded', 503);
          }),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(calls, 3); // maxAttempts parity with the Gemini path
      expect(out.retryable, isTrue);
    });

    test('validateKey: 200 accepted, 429 accepted (auth proven), 401 error',
        () async {
      final s = await settingsWith();
      Future<String?> validateWith(int status, String body) async {
        final a = OpenAiAnalyzer(s,
            client: MockClient((r) async => http.Response(body, status)),
            sleep: (_) async {},
            normalizer: (b) async => b);
        return a.validateKey('candidate');
      }

      expect(
          await OpenAiAnalyzer(s,
                  client: MockClient((r) async => _openaiOk('pong')),
                  sleep: (_) async {},
                  normalizer: (b) async => b)
              .validateKey('candidate'),
          isNull);
      expect(await validateWith(429, '{"error":{}}'), isNull);
      expect(await validateWith(401, '{"error":{}}'), isNotNull);
    });
  });

  group('AnthropicAnalyzer', () {
    test('vision request shape: x-api-key, version header, base64 block',
        () async {
      final s = await settingsWith(provider: 'anthropic');
      final requests = <http.Request>[];
      final a = AnthropicAnalyzer(s,
          client: MockClient((r) async {
            requests.add(r);
            return _anthropicOk(_foodJson);
          }),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(out.isFood, isTrue);
      final req = requests.single;
      expect(req.url.toString(), 'https://api.anthropic.com/v1/messages');
      expect(req.headers['x-api-key'], 'sk-an');
      expect(req.headers['anthropic-version'], '2023-06-01');
      final body = jsonDecode(req.body) as Map<String, dynamic>;
      expect(body['model'], 'claude-sonnet-5');
      final content = ((body['messages'] as List).first
          as Map<String, dynamic>)['content'] as List;
      expect((content.first as Map)['type'], 'image');
      expect((content.first as Map)['source']['media_type'], 'image/jpeg');
    });

    test('thinking-first content blocks: the TEXT block is extracted',
        () async {
      final s = await settingsWith(provider: 'anthropic');
      final a = AnthropicAnalyzer(s,
          client: MockClient((r) async => http.Response(
              jsonEncode({
                'content': [
                  {'type': 'thinking', 'thinking': ''},
                  {'type': 'text', 'text': _foodJson},
                ]
              }),
              200)),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      // Default model claude-sonnet-5 emits thinking blocks routinely —
      // content.first would classify a SUCCESSFUL reply as permanent
      // failure.
      expect(out.isFood, isTrue);
      expect(out.analysis!['total_calories'], 500);
    });

    test('validateKey accepts an HTTP 200 with NO text block (1-token reply)',
        () async {
      final s = await settingsWith(provider: 'anthropic');
      final a = AnthropicAnalyzer(s,
          client: MockClient((r) async => http.Response(
              jsonEncode({
                'content': [
                  {'type': 'thinking', 'thinking': 'x'}
                ]
              }),
              200)),
          sleep: (_) async {},
          normalizer: (b) async => b);
      expect(await a.validateKey('k'), isNull); // Gemini MAX_TOKENS parity
    });

    test('fenced JSON reply is parsed (no JSON mode on this API)', () async {
      final s = await settingsWith(provider: 'anthropic');
      final a = AnthropicAnalyzer(s,
          client: MockClient(
              (r) async => _anthropicOk('```json\n$_foodJson\n```')),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(out.isFood, isTrue);
      expect(out.analysis!['total_calories'], 500);
    });

    test('missing key reports retryable without any HTTP call', () async {
      final s = await settingsWith(provider: 'anthropic', anthropicKey: null);
      var calls = 0;
      final a = AnthropicAnalyzer(s,
          client: MockClient((r) async {
            calls++;
            return _anthropicOk(_foodJson);
          }),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(calls, 0);
      expect(out.retryable, isTrue); // user state, photo stays eligible
    });
  });

  /// Rebuilds a FACTORY-created analyzer with test seams (mock client, no
  /// isolate normalizer) while keeping every piece of factory config —
  /// endpoint, label, key/model slots, json-mode flag, extraBody, 404
  /// hint. Tests must go through this, not hand-rolled configs: a
  /// cross-wired factory (Doubao's keyOf pointing at the GLM slot) passed
  /// the whole suite when the test supplied its own lambdas.
  OpenAiCompatAnalyzer fromConfig(
          OpenAiCompatAnalyzer cfg, AppSettings s, http.Client client) =>
      OpenAiCompatAnalyzer(s,
          endpoint: cfg.endpoint,
          label: cfg.label,
          keyOf: cfg.keyOf,
          modelOf: cfg.modelOf,
          supportsJsonMode: cfg.supportsJsonMode,
          extraBody: cfg.extraBody,
          notFoundHint: cfg.notFoundHint,
          client: client,
          sleep: (_) async {},
          normalizer: (b) async => b);

  group('OpenAiCompatAnalyzer (mainland-China providers)', () {
    test('Qwen request shape: DashScope compat endpoint, bearer, data-URI, '
        'classic max_tokens', () async {
      final s = await settingsWith(provider: 'qwen', qwenKey: 'sk-qw');
      final requests = <http.Request>[];
      final direct = fromConfig(
          createQwenAnalyzer(s),
          s,
          MockClient((r) async {
            requests.add(r);
            return _openaiOk(_foodJson);
          }));
      final out = await direct.analyzePhoto(photo);
      expect(out.isFood, isTrue);
      final req = requests.single;
      expect(
          req.url.toString(),
          'https://dashscope.aliyuncs.com/compatible-mode/v1'
          '/chat/completions');
      expect(req.headers['Authorization'], 'Bearer sk-qw');
      final body = jsonDecode(req.body) as Map<String, dynamic>;
      expect(body['model'], 'qwen3-vl-flash');
      expect(body['response_format'], {'type': 'json_object'});
      // The compat layers accept the CLASSIC field; several reject the
      // newer max_completion_tokens with 400 — the opposite of OpenAI.
      expect(body['max_tokens'], isA<int>());
      expect(body.containsKey('max_completion_tokens'), isFalse);
      // qwen3-vl defaults to thinking OFF in compat mode — sending a
      // thinking field is unnecessary surface.
      expect(body.containsKey('thinking'), isFalse);
      final content = ((body['messages'] as List).first
          as Map<String, dynamic>)['content'] as List;
      expect((content[1] as Map)['image_url']['url'],
          startsWith('data:image/jpeg;base64,'));
    });

    test('each FACTORY reads its own key and model slot — cross-wiring '
        'fails here', () async {
      // All three keys and models configured DIFFERENTLY, then each
      // factory-config analyzer must put its own pair on the wire. The
      // old version of this test supplied its own keyOf/modelOf lambdas,
      // so a factory reading the wrong slot passed the entire suite
      // (empirically shown by the 2026-07-29 review).
      final s = await settingsWith(
          provider: 'qwen', qwenKey: 'k-qw', doubaoKey: 'k-db', glmKey: 'k-gl');
      await s.setQwenModel('m-qw');
      await s.setDoubaoModel('m-db');
      await s.setGlmModel('m-gl');
      final wire = <String, List<String?>>{};
      Future<void> run(OpenAiCompatAnalyzer cfg) async {
        await fromConfig(cfg, s, MockClient((r) async {
          final body = jsonDecode(r.body) as Map<String, dynamic>;
          wire[cfg.label] = [
            r.url.host,
            r.headers['Authorization'],
            body['model'] as String,
          ];
          return _openaiOk(_foodJson);
        })).analyzePhoto(photo);
      }

      await run(createQwenAnalyzer(s));
      await run(createDoubaoAnalyzer(s));
      await run(createGlmAnalyzer(s));
      expect(wire['Qwen'], ['dashscope.aliyuncs.com', 'Bearer k-qw', 'm-qw']);
      expect(wire['Doubao'],
          ['ark.cn-beijing.volces.com', 'Bearer k-db', 'm-db']);
      expect(wire['GLM'], ['open.bigmodel.cn', 'Bearer k-gl', 'm-gl']);
    });

    test('Doubao-config vision request: json mode AND thinking disabled '
        'together on the wire', () async {
      final s = await settingsWith(provider: 'doubao', doubaoKey: 'sk-db');
      final requests = <http.Request>[];
      final out = await fromConfig(
          createDoubaoAnalyzer(s),
          s,
          MockClient((r) async {
            requests.add(r);
            return _openaiOk(_foodJson);
          })).analyzePhoto(photo);
      expect(out.isFood, isTrue);
      final body = jsonDecode(requests.single.body) as Map<String, dynamic>;
      // Doubao is the unique combination: supportsJsonMode true PLUS
      // extraBody — a refactor entangling the two flags shipped green
      // before this test existed.
      expect(body['response_format'], {'type': 'json_object'});
      expect(body['thinking'], {'type': 'disabled'});
      expect(body['model'], 'doubao-seed-2-0-mini-260428');
      expect(body['max_tokens'], isA<int>(),
          reason: 'extras must never clobber the computed core fields');
    });

    test('a user-typed *thinking* model id suppresses thinking:disabled',
        () async {
      // Forced-thinking IDs (doubao-seed-*-thinking-*) reject disabled
      // with a hard 400 — the constant extraBody must yield to the model.
      final s = await settingsWith(provider: 'doubao', doubaoKey: 'sk-db');
      await s.setDoubaoModel('doubao-seed-1-6-thinking-250715');
      final requests = <http.Request>[];
      await fromConfig(createDoubaoAnalyzer(s), s, MockClient((r) async {
        requests.add(r);
        return _openaiOk(_foodJson);
      })).analyzePhoto(photo);
      final body = jsonDecode(requests.single.body) as Map<String, dynamic>;
      expect(body.containsKey('thinking'), isFalse);
    });

    test('textIntent (no image) sends NO response_format and no image part',
        () async {
      final s = await settingsWith(provider: 'qwen', qwenKey: 'sk-qw');
      final requests = <http.Request>[];
      await fromConfig(createQwenAnalyzer(s), s, MockClient((r) async {
        requests.add(r);
        return _openaiOk('{"actions": []}');
      })).textIntent('log a latte');
      final body = jsonDecode(requests.single.body) as Map<String, dynamic>;
      // json_object on a TEXT call would 400 the moment an intent prompt
      // lacks the literal word "json" — the guard is jpegBytes != null.
      expect(body.containsKey('response_format'), isFalse);
      final content = ((body['messages'] as List).single
          as Map<String, dynamic>)['content'] as List;
      expect(content, hasLength(1));
      expect((content.single as Map)['type'], 'text');
    });

    test('factory config: GLM has NO json mode; Doubao and GLM disable '
        'thinking; endpoints and labels pinned', () async {
      final s = await settingsWith(provider: 'qwen');
      final qwen = createQwenAnalyzer(s);
      final doubao = createDoubaoAnalyzer(s);
      final glm = createGlmAnalyzer(s);
      expect(
          qwen.endpoint.toString(),
          'https://dashscope.aliyuncs.com/compatible-mode/v1'
          '/chat/completions');
      expect(doubao.endpoint.toString(),
          'https://ark.cn-beijing.volces.com/api/v3/chat/completions');
      expect(glm.endpoint.toString(),
          'https://open.bigmodel.cn/api/paas/v4/chat/completions');
      expect([qwen.label, doubao.label, glm.label], ['Qwen', 'Doubao', 'GLM']);
      // Zhipu's API reference marks response_format TEXT-models-only —
      // a vision request carrying it is the drift this test exists for.
      expect(glm.supportsJsonMode, isFalse);
      expect(qwen.supportsJsonMode, isTrue);
      expect(doubao.supportsJsonMode, isTrue);
      // Doubao/GLM vision models THINK by default; a fixed-schema
      // extraction pays reasoning tokens for nothing.
      const off = {
        'thinking': {'type': 'disabled'}
      };
      expect(doubao.extraBody, off);
      expect(glm.extraBody, off);
      expect(qwen.extraBody, isEmpty);
    });

    test('GLM-config vision request: no response_format, thinking disabled',
        () async {
      final s = await settingsWith(provider: 'glm', glmKey: 'zk');
      final requests = <http.Request>[];
      final a = fromConfig(
          createGlmAnalyzer(s),
          s,
          MockClient((r) async {
            requests.add(r);
            return _openaiOk('```json\n$_foodJson\n```');
          }));
      final out = await a.analyzePhoto(photo);
      expect(out.isFood, isTrue, reason: 'fenced JSON still parses');
      final body = jsonDecode(requests.single.body) as Map<String, dynamic>;
      expect(body.containsKey('response_format'), isFalse);
      expect(body['thinking'], {'type': 'disabled'});
      expect(body['model'], 'glm-4.6v-flash');
    });

    test('supportsJsonMode:false omits response_format entirely', () async {
      final s = await settingsWith(provider: 'qwen', qwenKey: 'sk-qw');
      final requests = <http.Request>[];
      final a = OpenAiCompatAnalyzer(s,
          endpoint: Uri.parse('https://example.invalid/v1/chat/completions'),
          label: 'X',
          keyOf: (x) => x.qwenApiKey,
          modelOf: (x) => x.qwenModel,
          supportsJsonMode: false,
          client: MockClient((r) async {
            requests.add(r);
            return _openaiOk('```json\n$_foodJson\n```');
          }),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(out.isFood, isTrue, reason: 'fenced JSON still parses');
      final body = jsonDecode(requests.single.body) as Map<String, dynamic>;
      expect(body.containsKey('response_format'), isFalse);
    });

    test('429 is retryable and quota-classed; validateKey accepts it; the '
        'DAILY latch stays un-armed', () async {
      final s = await settingsWith(provider: 'qwen', qwenKey: 'sk-qw');
      var calls = 0;
      final a = OpenAiCompatAnalyzer(s,
          endpoint: Uri.parse('https://example.invalid/v1/chat/completions'),
          label: 'X',
          keyOf: (x) => x.qwenApiKey,
          modelOf: (x) => x.qwenModel,
          client: MockClient((r) async {
            calls++;
            return http.Response('{"error":{}}', 429);
          }),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(out.retryable, isTrue);
      expect(calls, 1, reason: 'quota-class never retries in place');
      // Design invariant: the China trio's limits are RPM/TPM
      // (minute-scale) — like OpenAI they must NOT arm the daily
      // quota-pause latch; only Gemini's daily free-tier cap does. A
      // "consistency" refactor arming it here would dead-air ALL
      // analysis for hours after one rate blip.
      expect(s.quotaPauseUntil, isNull);
      expect(s.isQuotaPaused, isFalse);
      expect(await a.validateKey('candidate'), isNull,
          reason: 'a quota-class reply proves the key authenticated');
    });

    test('billing-dead signatures: GLM 429 code 1113, Doubao 403 '
        'AccountOverdue, Qwen 400 Arrearage — all retryable billing '
        'messages, keys stay accepted', () async {
      final s = await settingsWith(
          provider: 'glm', glmKey: 'zk', doubaoKey: 'dk', qwenKey: 'qk');
      Future<void> check(OpenAiCompatAnalyzer cfg, int status, String body,
          {required String label}) async {
        // charset=utf-8 like the real APIs — http.Response's latin1
        // default cannot encode the Chinese error messages.
        final a = fromConfig(
            cfg,
            s,
            MockClient((r) async => http.Response(body, status, headers: {
                  'content-type': 'application/json; charset=utf-8'
                })));
        final out = await a.analyzePhoto(photo);
        expect(out.retryable, isTrue,
            reason: '$label: a recharge fixes it — the photo must stay '
                'eligible, not burn to failed');
        expect(out.error, contains('billing'), reason: label);
        expect(out.error, contains(cfg.label), reason: label);
        expect(await a.validateKey('k'), isNull,
            reason: '$label: the key AUTHENTICATED — rejecting it sends '
                'the user to regenerate a working key');
      }

      // Zhipu: insufficient balance is HTTP 429 code 1113 — NOT a rate
      // limit; the generic "will retry later" message looped forever.
      await check(createGlmAnalyzer(s), 429,
          '{"error":{"code":"1113","message":"余额不足或无可用资源包，请充值"}}',
          label: 'GLM 1113');
      // Ark: an overdue account is HTTP 403 — NOT auth; the old mapping
      // said "key rejected" and burned the photo non-retryably.
      await check(
          createDoubaoAnalyzer(s),
          403,
          '{"error":{"code":"AccountOverdueError","message":"overdue '
              'balance"}}',
          label: 'Doubao 403');
      // DashScope: arrears is HTTP 400 code Arrearage — NOT a client bug.
      await check(createQwenAnalyzer(s), 400,
          '{"error":{"code":"Arrearage","message":"Access denied"}}',
          label: 'Qwen Arrearage');
    });

    test('a PLAIN 403 (no billing signature) is still an auth rejection',
        () async {
      final s = await settingsWith(provider: 'doubao', doubaoKey: 'dk');
      final a = fromConfig(createDoubaoAnalyzer(s), s,
          MockClient((r) async => http.Response('{"error":{}}', 403)));
      final out = await a.analyzePhoto(photo);
      expect(out.retryable, isFalse);
      expect(out.error, contains('rejected'));
    });

    test('404 explains the MODEL, not the key (Doubao activation rule)',
        () async {
      final s = await settingsWith(provider: 'doubao', doubaoKey: 'dk');
      final a = fromConfig(createDoubaoAnalyzer(s), s,
          MockClient((r) async => http.Response('{"error":{}}', 404)));
      final out = await a.analyzePhoto(photo);
      // The generic 'Provider request failed: HTTP 404' rendered under
      // the KEY field — users regenerated working keys over a model id.
      expect(out.error, contains('model'));
      expect(out.error, contains('开通'));
      expect(await a.validateKey('k'), contains('model'),
          reason: 'validate must point at the model too');
    });

    test('missing key names the provider in the Settings message', () async {
      final s = await settingsWith(provider: 'qwen');
      var calls = 0;
      final a = OpenAiCompatAnalyzer(s,
          endpoint: Uri.parse('https://example.invalid/v1/chat/completions'),
          label: 'Qwen',
          keyOf: (x) => x.qwenApiKey,
          modelOf: (x) => x.qwenModel,
          client: MockClient((r) async {
            calls++;
            return _openaiOk(_foodJson);
          }),
          sleep: (_) async {},
          normalizer: (b) async => b);
      final out = await a.analyzePhoto(photo);
      expect(calls, 0);
      expect(out.retryable, isTrue);
      expect(out.error, contains('Qwen'));
    });
  });

  group('MultiProviderAnalyzer delegation', () {
    test('routes by settings.provider PER CALL — switch needs no rewiring',
        () async {
      final s = await settingsWith(
          provider: 'openai',
          geminiKey: 'g-key',
          qwenKey: 'q',
          doubaoKey: 'd',
          glmKey: 'z');
      final hosts = <String>[];
      final client = MockClient((r) async {
        hosts.add(r.url.host);
        if (r.url.host.contains('openai')) return _openaiOk(_foodJson);
        if (r.url.host.contains('anthropic')) return _anthropicOk(_foodJson);
        if (r.url.host.contains('googleapis')) {
          return http.Response(
              jsonEncode({
                'candidates': [
                  {
                    'content': {
                      'parts': [
                        {'text': _foodJson}
                      ]
                    }
                  }
                ]
              }),
              200);
        }
        return _openaiOk(_foodJson); // the compat providers
      });
      final multi = MultiProviderAnalyzer(s, client: client);

      await multi.textIntent('ping1');
      await s.setProvider(AiProvider.anthropic);
      await multi.textIntent('ping2');
      await s.setProvider(AiProvider.gemini);
      await multi.textIntent('ping3');
      await s.setProvider(AiProvider.qwen);
      await multi.textIntent('ping4');
      await s.setProvider(AiProvider.doubao);
      await multi.textIntent('ping5');
      await s.setProvider(AiProvider.glm);
      await multi.textIntent('ping6');

      expect(hosts, [
        'api.openai.com',
        'api.anthropic.com',
        'generativelanguage.googleapis.com',
        'dashscope.aliyuncs.com',
        'ark.cn-beijing.volces.com',
        'open.bigmodel.cn',
      ]);
    });
  });

  group('provider settings', () {
    test('per-provider keys/models persist and activeApiKey follows provider',
        () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final keys = MemKeys();
      final s = await AppSettings.load(prefs: prefs, keyStore: keys);
      await s.setGeminiApiKey('g');
      await s.setOpenaiApiKey('o');
      await s.setAnthropicApiKey('a');
      await s.setOpenaiModel('gpt-x');

      expect(s.activeApiKey, 'g'); // default provider gemini
      await s.setProvider(AiProvider.openai);
      expect(s.activeApiKey, 'o');
      expect(s.activeModel, 'gpt-x');

      final again = await AppSettings.load(prefs: prefs, keyStore: keys);
      expect(again.provider, AiProvider.openai);
      expect(again.openaiApiKey, 'o');
      expect(again.anthropicApiKey, 'a');
      expect(again.openaiModel, 'gpt-x');
    });

    test('the China-provider trio persists keys/models like the others',
        () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final keys = MemKeys();
      final s = await AppSettings.load(prefs: prefs, keyStore: keys);
      await s.setQwenApiKey('qk');
      await s.setDoubaoApiKey('dk');
      await s.setGlmApiKey('zk');
      await s.setQwenModel('qwen-vl-max');

      await s.setProvider(AiProvider.qwen);
      expect(s.activeApiKey, 'qk');
      expect(s.activeModel, 'qwen-vl-max');
      await s.setProvider(AiProvider.doubao);
      expect(s.activeApiKey, 'dk');
      expect(s.activeModel, AppSettings.defaultDoubaoModel);
      await s.setProvider(AiProvider.glm);
      expect(s.activeApiKey, 'zk');
      expect(s.activeModel, AppSettings.defaultGlmModel);

      // Keys live in SECURE storage, never shared_preferences.
      expect(keys.data['qwen_api_key'], 'qk');
      expect(prefs.getString('settings.qwen_model'), 'qwen-vl-max');
      expect(prefs.getKeys().where((k) => k.contains('key')), isEmpty);

      final again = await AppSettings.load(prefs: prefs, keyStore: keys);
      expect(again.qwenApiKey, 'qk');
      expect(again.doubaoApiKey, 'dk');
      expect(again.glmApiKey, 'zk');
      expect(again.qwenModel, 'qwen-vl-max');
    });

    test('a latch armed FOR gemini never pauses another provider', () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final s = await AppSettings.load(prefs: prefs, keyStore: MemKeys());
      await s.setProvider(AiProvider.openai);
      // An in-flight Gemini call arming its latch AFTER the user switched:
      await s.setQuotaPauseUntil(DateTime.now().add(const Duration(hours: 6)),
          forProvider: AiProvider.gemini);
      expect(s.isQuotaPaused, isFalse); // OpenAI unaffected
      await s.setProvider(AiProvider.gemini);
      expect(s.isQuotaPaused, isFalse,
          reason: 'setProvider(change) clears the latch entirely');
    });

    test('switching provider clears the quota-pause latch', () async {
      SharedPreferences.setMockInitialValues({});
      final prefs = await SharedPreferences.getInstance();
      final s =
          await AppSettings.load(prefs: prefs, keyStore: MemKeys());
      await s.setQuotaPauseUntil(
          DateTime.now().add(const Duration(hours: 6)));
      await s.setProvider(AiProvider.openai);
      expect(s.quotaPauseUntil, isNull);
    });
  });
}
