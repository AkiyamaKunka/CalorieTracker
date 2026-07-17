import 'dart:convert';
import 'dart:typed_data';

import 'package:calorie_tracker/services/analyzer/gemini_analyzer.dart';
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

/// Gemini 200 body whose candidate text is [text].
String geminiOk(String text) => jsonEncode({
      'candidates': [
        {
          'content': {
            'parts': [
              {'text': text}
            ]
          }
        }
      ]
    });

const String dailyQuotaBody =
    '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": '
    '"Quota exceeded for quota metric generate_content_free_tier_requests '
    'limit GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}';

const String foodJson = '{"is_food": true, "food_items": '
    '[{"name": "Toast", "estimated_calories": 120}], "total_calories": 120, '
    '"meal_description": "Toast"}';

Future<AppSettings> freshSettings({String? apiKey = 'test-key'}) async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  final s = await AppSettings.load(prefs: prefs, keyStore: MemoryKeyStore());
  if (apiKey != null) await s.setGeminiApiKey(apiKey);
  return s;
}

/// Test harness: records every request and every sleep; identity normalizer
/// by default (normalization itself is covered in normalize_test.dart).
class Harness {
  Harness(this.settings, List<http.Response> responses,
      {Future<Uint8List?> Function(Uint8List)? normalizer}) {
    analyzer = GeminiAnalyzer(
      settings,
      client: MockClient((req) async {
        requests.add(req);
        if (responses.isEmpty) fail('unexpected extra HTTP request');
        return responses.removeAt(0);
      }),
      sleep: (d) async => sleeps.add(d),
      normalizer: normalizer ?? (b) async => b,
    );
  }

  final AppSettings settings;
  final List<http.Request> requests = [];
  final List<Duration> sleeps = [];
  late final GeminiAnalyzer analyzer;
}

http.Response ok(String text) => http.Response(geminiOk(text), 200,
    headers: {'content-type': 'application/json; charset=utf-8'});

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final photo = Uint8List.fromList(List.generate(32, (i) => i));

  group('request shape (spec §3.2)', () {
    test('photo call: endpoint, key, parts, and JSON response mode', () async {
      final s = await freshSettings();
      final h = Harness(s, [ok(foodJson)]);
      final out = await h.analyzer.analyzePhoto(photo);

      expect(out.error, isNull);
      expect(out.isFood, isTrue);
      expect(out.analysis!['total_calories'], 120);

      final req = h.requests.single;
      expect(req.method, 'POST');
      expect(req.url.toString(),
          startsWith('https://generativelanguage.googleapis.com/v1beta/models/'
              'gemini-2.5-flash:generateContent'));
      expect(req.url.queryParameters['key'], 'test-key');

      final body = jsonDecode(req.body) as Map<String, dynamic>;
      expect(body['generationConfig'],
          {'response_mime_type': 'application/json'});
      final parts = body['contents'][0]['parts'] as List;
      expect(parts, hasLength(2));
      expect(parts[0]['text'], contains('Analyze this photo'));
      expect(parts[1]['inline_data']['mime_type'], 'image/jpeg');
      expect(parts[1]['inline_data']['data'], base64Encode(photo));
    });

    test('dietary profile appends to the photo prompt only (spec §1.3)',
        () async {
      final s = await freshSettings();
      await s.setDietaryProfile('halal');
      final h = Harness(s, [ok(foodJson)]);
      await h.analyzer.analyzePhoto(photo);
      final prompt =
          jsonDecode(h.requests.single.body)['contents'][0]['parts'][0]['text']
              as String;
      expect(prompt, contains("User's Dietary Profile"));
      expect(prompt, contains('halal'));
    });

    test('no API key configured fails without any HTTP call', () async {
      final s = await freshSettings(apiKey: null);
      final h = Harness(s, []);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.error, contains('key'));
      expect(h.requests, isEmpty);
    });
  });

  group('is_food coercion gate (core/coerce parity)', () {
    test('boolish string is coerced, analysis otherwise untouched', () async {
      final s = await freshSettings();
      final h = Harness(s, [
        ok('{"is_food": "yes", "total_calories": "lots", '
            '"food_items": "junk"}')
      ]);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.isFood, isTrue);
      expect(out.analysis!['is_food'], true);
      // No mutation beyond is_food: hostile fields stay raw for consumers.
      expect(out.analysis!['total_calories'], 'lots');
      expect(out.analysis!['food_items'], 'junk');
    });

    test('is_food false is a successful not-food outcome', () async {
      final s = await freshSettings();
      final h = Harness(s, [ok('{"is_food": false}')]);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.error, isNull);
      expect(out.isFood, isFalse);
      expect(out.analysis, isNotNull);
    });

    test('uncoercible or missing is_food rejects the analysis', () async {
      for (final text in [
        '{"is_food": "maybe"}',
        '{"total_calories": 500}',
        '[1, 2, 3]',
        '"just a string"',
      ]) {
        final s = await freshSettings();
        final h = Harness(s, [ok(text)]);
        final out = await h.analyzer.analyzePhoto(photo);
        expect(out.analysis, isNull, reason: text);
        expect(out.error, isNotNull, reason: text);
      }
    });

    test('markdown-fenced JSON is parsed (spec §3.4)', () async {
      final s = await freshSettings();
      final h = Harness(s, [ok('```json\n$foodJson\n```')]);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.isFood, isTrue);
    });
  });

  group('retry / backoff (spec §3.3)', () {
    test('parse errors are never retried', () async {
      final s = await freshSettings();
      final h = Harness(s, [ok('utter nonsense, no json here')]);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.error, contains("Couldn't understand"));
      expect(h.requests, hasLength(1));
    });

    test('503 retries then succeeds; default 5s backoff', () async {
      final s = await freshSettings();
      final h = Harness(s, [
        http.Response('{"error":{"code":503,"status":"UNAVAILABLE"}}', 503),
        http.Response('{"error":{"code":503,"status":"UNAVAILABLE"}}', 503),
        ok(foodJson),
      ]);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.isFood, isTrue);
      expect(h.requests, hasLength(3));
      expect(h.sleeps, [const Duration(seconds: 5), const Duration(seconds: 5)]);
    });

    test('persistent 503 stops after 3 attempts', () async {
      final s = await freshSettings();
      final h = Harness(s, List.generate(3,
          (_) => http.Response('{"error":{"status":"UNAVAILABLE"}}', 503)));
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.error, isNotNull);
      expect(h.requests, hasLength(3));
    });

    test('429 honors the server retryDelay hint', () async {
      final s = await freshSettings();
      final h = Harness(s, [
        http.Response(
            '{"error":{"code":429,"status":"RESOURCE_EXHAUSTED",'
            '"details":[{"retryDelay":"12s"}]}}',
            429),
        ok(foodJson),
      ]);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.isFood, isTrue);
      expect(h.sleeps, [const Duration(seconds: 12)]);
    });

    test('auth errors are not retried', () async {
      final s = await freshSettings();
      final h = Harness(s, [
        http.Response('{"error":{"code":403,"status":"PERMISSION_DENIED"}}',
            403)
      ]);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.error, contains('key'));
      expect(h.requests, hasLength(1));
    });

    test('classification table matches the spec', () {
      expect(classifyGeminiError('HTTP 429: $dailyQuotaBody'),
          GeminiErrorClass.dailyQuotaExhausted);
      expect(classifyGeminiError('HTTP 429: RATE_LIMIT'),
          GeminiErrorClass.quotaRateLimit);
      expect(classifyGeminiError('HTTP 401: UNAUTHENTICATED'),
          GeminiErrorClass.auth);
      expect(classifyGeminiError('CONNECTION error: socket closed'),
          GeminiErrorClass.networkService);
      expect(classifyGeminiError('DEADLINE: client-side 90s deadline hit'),
          GeminiErrorClass.networkService);
      expect(classifyGeminiError('HTTP 404: MODEL_NOT_FOUND'),
          GeminiErrorClass.modelError);
      expect(classifyGeminiError('weird'), GeminiErrorClass.unknown);
    });

    test('retryable classes per spec: 5xx/network/timeout yes, daily-quota no',
        () {
      expect(isRetryableGeminiError('HTTP 503: UNAVAILABLE'), isTrue);
      expect(isRetryableGeminiError('HTTP 500: internal'), isTrue);
      expect(isRetryableGeminiError('HTTP 429: RESOURCE_EXHAUSTED'), isTrue);
      expect(isRetryableGeminiError('DEADLINE hit'), isTrue);
      expect(isRetryableGeminiError('CONNECTION error: refused'), isTrue);
      expect(isRetryableGeminiError('HTTP 429: $dailyQuotaBody'), isFalse);
      expect(isRetryableGeminiError('HTTP 403: PERMISSION_DENIED'), isFalse);
      expect(isRetryableGeminiError('HTTP 400: INVALID_ARGUMENT'), isFalse);
    });

    test('retryDelay parsing: forms, ceil, clamp [1,60], default 5', () {
      expect(parseRetryDelaySeconds('retryDelay: "7s"'), 7);
      expect(parseRetryDelaySeconds("retryDelay: '2.2s'"), 3); // ceil
      expect(parseRetryDelaySeconds('please retry in 9s'), 9);
      expect(parseRetryDelaySeconds('retry in 1500 ms'), 2);
      expect(parseRetryDelaySeconds('retry in 1 ms'), 1); // clamp low
      expect(parseRetryDelaySeconds('retryDelay: "600s"'), 60); // clamp high
      expect(parseRetryDelaySeconds('no hint at all'), 5);
    });
  });

  group('daily-quota pause latch (spec §3.3)', () {
    test('daily quota error latches ~12h pause, no retry', () async {
      final s = await freshSettings();
      final h = Harness(s, [http.Response(dailyQuotaBody, 429)]);
      final before = DateTime.now();
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.error, contains('paused'));
      expect(h.requests, hasLength(1)); // never retried
      final until = s.quotaPauseUntil!;
      expect(until.difference(before),
          allOf(greaterThan(const Duration(hours: 11, minutes: 59)),
              lessThan(const Duration(hours: 12, minutes: 1))));
    });

    test('while paused, analyzePhoto returns immediately with no HTTP call',
        () async {
      final s = await freshSettings();
      await s.setQuotaPauseUntil(DateTime.now().add(const Duration(hours: 2)));
      final h = Harness(s, []);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.error, contains('paused'));
      expect(h.requests, isEmpty);
    });

    test('an expired pause does not block, and any success clears the latch',
        () async {
      final s = await freshSettings();
      await s.setQuotaPauseUntil(
          DateTime.now().subtract(const Duration(minutes: 1)));
      final h = Harness(s, [ok(foodJson)]);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.isFood, isTrue);
      expect(s.quotaPauseUntil, isNull); // spec: later success clears pause
    });

    test('textIntent daily-quota failure latches the pause too', () async {
      final s = await freshSettings();
      final h = Harness(s, [http.Response(dailyQuotaBody, 429)]);
      expect(await h.analyzer.textIntent('hello'), isNull);
      expect(s.isQuotaPaused, isTrue);
    });
  });

  group('normalization fallback (spec §3.1 step 6)', () {
    test('normalize failure with small original sends original bytes',
        () async {
      final s = await freshSettings();
      final h = Harness(s, [ok(foodJson)], normalizer: (_) async => null);
      final out = await h.analyzer.analyzePhoto(photo);
      expect(out.isFood, isTrue);
      final data = jsonDecode(h.requests.single.body)['contents'][0]['parts']
          [1]['inline_data']['data'] as String;
      expect(data, base64Encode(photo));
    });

    test('normalize failure with >=5MB original fails with no HTTP call',
        () async {
      final s = await freshSettings();
      final h = Harness(s, [], normalizer: (_) async => null);
      final big = Uint8List(5 * 1024 * 1024);
      final out = await h.analyzer.analyzePhoto(big);
      expect(out.error, isNotNull);
      expect(out.analysis, isNull);
      expect(h.requests, isEmpty);
    });
  });

  group('textIntent (spec §3.2 text call / §4.1 shape carriage)', () {
    test('sends a single text part and returns the parsed map', () async {
      final s = await freshSettings();
      final h = Harness(
          s, [ok('{"intent": "chat", "reply": "hi"}')]);
      final out = await h.analyzer.textIntent('the prompt');
      expect(out, {'intent': 'chat', 'reply': 'hi'});
      final parts =
          jsonDecode(h.requests.single.body)['contents'][0]['parts'] as List;
      expect(parts, hasLength(1));
      expect(parts[0]['text'], 'the prompt');
    });

    test('bare array survives the Map seam as {actions: [...]}', () async {
      final s = await freshSettings();
      final h = Harness(s, [
        ok('[{"intent": "correction", "meal_index": 0},'
            ' {"intent": "delete", "meal_indices": [1]}]')
      ]);
      final out = await h.analyzer.textIntent('p');
      expect(out!.keys, ['actions']);
      expect((out['actions'] as List), hasLength(2));
    });

    test('transport errors, parse errors, and scalar JSON return null',
        () async {
      final s = await freshSettings();
      for (final resp in [
        http.Response('{"error":{"status":"UNAVAILABLE"}}', 503),
        ok('not json at all'),
        ok('42'),
      ]) {
        final h = Harness(s, [resp]);
        expect(await h.analyzer.textIntent('p'), isNull);
        expect(h.requests, hasLength(1)); // text path: single attempt
      }
    });
  });

  group('validateKey (spec §3: 1-token real generation)', () {
    test('200 → null, uses the candidate key and maxOutputTokens 1', () async {
      final s = await freshSettings(apiKey: null); // no stored key needed
      final h = Harness(s, [ok('')]);
      expect(await h.analyzer.validateKey('candidate-key'), isNull);
      final req = h.requests.single;
      expect(req.url.queryParameters['key'], 'candidate-key');
      expect(jsonDecode(req.body)['generationConfig'], {'maxOutputTokens': 1});
    });

    test('MAX_TOKENS body with no text part still validates', () async {
      final s = await freshSettings(apiKey: null);
      final h = Harness(s, [
        http.Response(
            '{"candidates":[{"finishReason":"MAX_TOKENS","content":{}}]}', 200)
      ]);
      expect(await h.analyzer.validateKey('k'), isNull);
    });

    test('rejected key returns a user-facing error', () async {
      final s = await freshSettings(apiKey: null);
      final h = Harness(s, [
        http.Response(
            '{"error":{"code":400,"status":"INVALID_ARGUMENT",'
            '"message":"API_KEY_INVALID"}}',
            400)
      ]);
      final msg = await h.analyzer.validateKey('bad');
      expect(msg, isNotNull);
      expect(msg, contains('key'));
    });
  });
}
