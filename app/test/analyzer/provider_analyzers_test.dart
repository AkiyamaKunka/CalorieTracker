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
}) async {
  SharedPreferences.setMockInitialValues({'settings.provider': provider});
  final prefs = await SharedPreferences.getInstance();
  final keys = MemKeys();
  if (openaiKey != null) keys.data['openai_api_key'] = openaiKey;
  if (anthropicKey != null) keys.data['anthropic_api_key'] = anthropicKey;
  if (geminiKey != null) keys.data['gemini_api_key'] = geminiKey;
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

  group('MultiProviderAnalyzer delegation', () {
    test('routes by settings.provider PER CALL — switch needs no rewiring',
        () async {
      final s = await settingsWith(provider: 'openai', geminiKey: 'g-key');
      final hosts = <String>[];
      final client = MockClient((r) async {
        hosts.add(r.url.host);
        if (r.url.host.contains('openai')) return _openaiOk(_foodJson);
        if (r.url.host.contains('anthropic')) return _anthropicOk(_foodJson);
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
      });
      final multi = MultiProviderAnalyzer(s, client: client);

      await multi.textIntent('ping1');
      await s.setProvider(AiProvider.anthropic);
      await multi.textIntent('ping2');
      await s.setProvider(AiProvider.gemini);
      await multi.textIntent('ping3');

      expect(hosts, [
        'api.openai.com',
        'api.anthropic.com',
        'generativelanguage.googleapis.com',
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
