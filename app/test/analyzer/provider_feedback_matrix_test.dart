// PROVIDER-FEEDBACK CONFORMANCE MATRIX (user request 2026-07-30: "I doubt
// they use the same return value or format of feedback").
//
// They don't. Same condition, seven different wire shapes — e.g. "account
// out of money" arrives as OpenAI 429 insufficient_quota, Anthropic 400
// "credit balance is too low", GLM 429 code "1113", Doubao 403
// AccountOverdueError, Qwen 400 "Arrearage". This file is the ONE place
// that proves each provider's real feedback lands in the app's shared
// outcome vocabulary (retryable / auth / billing / latch) correctly.
//
// Reusability contract: adding a provider = one ProviderSpec entry; adding
// a failure class = one Scenario + a fixture per provider. Fixture bodies
// are VERBATIM from provider docs or live probes (the qwen/doubao/glm 401
// bodies were captured live from the real endpoints on 2026-07-30).
import 'dart:convert';
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/analyzer/gemini_analyzer.dart';
import 'package:calorie_tracker/services/analyzer/provider_analyzers.dart';
import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'server_analyzer_test.dart' show MemoryKeyStore;

const _utf8Json = {'content-type': 'application/json; charset=utf-8'};

const _goodJson = '{"is_food": true, "meal_description": "Beef noodles", '
    '"total_calories": 620, "total_protein_g": 32, "total_carbs_g": 70, '
    '"total_fat_g": 20, "food_items": []}';

/// What the app must conclude from a provider's reply. One vocabulary for
/// all seven providers — that IS the reusability.
enum Scenario {
  auth, // key rejected → permanent, message points at the key
  rateLimit, // slow down → retryable, exactly one call, NO daily latch
  billing, // out of money → retryable (recharge fixes), says "billing"
  modelNotFound, // bad/retired model → permanent (retry can't help)
  overloaded, // provider-side trouble → retryable
  contentFilter, // refused input → permanent (a retry loops forever)
  junkReply, // HTTP 200 but non-JSON prose → permanent, no analysis
  dailyQuota, // Gemini free tier only: arms the persisted pause latch
}

class Fx {
  const Fx(this.status, this.body, {this.expectSays, this.expectCalls});
  final int status;
  final String body; // VERBATIM provider bodies — do not tidy them
  final String? expectSays; // provider-specific message fragment
  final int? expectCalls; // pin call count where retry-burn matters
}

class ProviderSpec {
  const ProviderSpec({
    required this.name,
    required this.make,
    required this.settings,
    required this.wrapSuccess,
    required this.fx,
    this.supportsFencedReply = true,
  });
  final String name;
  final AnalyzerService Function(AppSettings, http.Client) make;
  final Future<AppSettings> Function() settings;
  final String Function(String innerText) wrapSuccess;
  final Map<Scenario, Fx> fx;
  final bool supportsFencedReply; // server pre-parses: fences impossible
}

Future<AppSettings> _settingsFor(AiProvider p) async {
  SharedPreferences.setMockInitialValues({});
  final s = await AppSettings.load(
      prefs: await SharedPreferences.getInstance(),
      keyStore: MemoryKeyStore());
  await s.setProvider(p);
  switch (p) {
    case AiProvider.gemini:
      await s.setGeminiApiKey('gk');
    case AiProvider.openai:
      await s.setOpenaiApiKey('ok');
    case AiProvider.anthropic:
      await s.setAnthropicApiKey('ak');
    case AiProvider.server:
      await s.setServerBaseUrl('http://10.0.0.5');
      await s.setServerApiKey('uk');
    case AiProvider.qwen:
      await s.setQwenApiKey('qk');
    case AiProvider.doubao:
      await s.setDoubaoApiKey('dk');
    case AiProvider.glm:
      await s.setGlmApiKey('zk');
  }
  return s;
}

String _openaiShape(String text) => jsonEncode({
      'choices': [
        {
          'message': {'role': 'assistant', 'content': text}
        }
      ]
    });

OpenAiCompatAnalyzer _compat(
        OpenAiCompatAnalyzer cfg, AppSettings s, http.Client c) =>
    OpenAiCompatAnalyzer(s,
        endpoint: cfg.endpoint,
        label: cfg.label,
        keyOf: cfg.keyOf,
        modelOf: cfg.modelOf,
        supportsJsonMode: cfg.supportsJsonMode,
        extraBody: cfg.extraBody,
        notFoundHint: cfg.notFoundHint,
        client: c,
        sleep: (_) async {},
        normalizer: (b) async => b);

void main() {
  final photo = Uint8List.fromList(List.filled(64, 7));

  final providers = <ProviderSpec>[
    ProviderSpec(
      name: 'Gemini',
      settings: () => _settingsFor(AiProvider.gemini),
      make: (s, c) => GeminiAnalyzer(s,
          client: c, sleep: (_) async {}, normalizer: (b) async => b),
      wrapSuccess: (text) => jsonEncode({
        'candidates': [
          {
            'content': {
              'parts': [
                {'text': text}
              ]
            }
          }
        ]
      }),
      fx: {
        Scenario.auth: const Fx(
            400,
            '{"error": {"code": 400, "message": "API key not valid. Please '
            'pass a valid API key. [reason: API_KEY_INVALID]", '
            '"status": "INVALID_ARGUMENT"}}',
            expectSays: 'key'),
        Scenario.rateLimit: const Fx(
            429,
            '{"error": {"code": 429, "message": "Resource has been '
            'exhausted (e.g. check quota).", "status": '
            '"RESOURCE_EXHAUSTED"}}'),
        Scenario.dailyQuota: const Fx(
            429,
            '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
            '"message": "You exceeded your current quota. Please migrate '
            'to a paid tier. Quota exceeded for metric: '
            'GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20"}}'),
        Scenario.overloaded: const Fx(
            503,
            '{"error": {"code": 503, "message": "The model is overloaded. '
            'Please try again later.", "status": "UNAVAILABLE"}}'),
        Scenario.modelNotFound: const Fx(
            404,
            '{"error": {"code": 404, "message": "models/gemini-9.9-nope is '
            'not found for API version v1beta, or is not supported for '
            'generateContent.", "status": "NOT_FOUND"}}',
            expectSays: 'model'),
      },
    ),
    ProviderSpec(
      name: 'OpenAI',
      settings: () => _settingsFor(AiProvider.openai),
      make: (s, c) => OpenAiAnalyzer(s,
          client: c, sleep: (_) async {}, normalizer: (b) async => b),
      wrapSuccess: _openaiShape,
      fx: {
        Scenario.auth: const Fx(
            401,
            '{"error": {"message": "Incorrect API key provided: sk-test*. '
            'You can find your API key at '
            'https://platform.openai.com/account/api-keys.", "type": '
            '"invalid_request_error", "param": null, "code": '
            '"invalid_api_key"}}',
            expectSays: 'key'),
        Scenario.rateLimit: const Fx(
            429,
            '{"error": {"message": "Rate limit reached for gpt-4o-mini in '
            'organization org-x on tokens per min (TPM).", "type": '
            '"rate_limit_error", "code": "rate_limit_exceeded"}}',
            expectCalls: 1),
        Scenario.billing: const Fx(
            429,
            '{"error": {"message": "You exceeded your current quota, '
            'please check your plan and billing details.", "type": '
            '"insufficient_quota", "code": "insufficient_quota"}}'),
        Scenario.modelNotFound: const Fx(
            404,
            '{"error": {"message": "The model \'gpt-nope\' does not exist '
            'or you do not have access to it.", "type": '
            '"invalid_request_error", "code": "model_not_found"}}'),
        Scenario.overloaded: const Fx(
            500,
            '{"error": {"message": "The server had an error while '
            'processing your request. Sorry about that!", "type": '
            '"server_error"}}',
            expectCalls: 3),
        Scenario.junkReply: const Fx(200, ''), // body built by the engine
      },
    ),
    ProviderSpec(
      name: 'Anthropic',
      settings: () => _settingsFor(AiProvider.anthropic),
      make: (s, c) => AnthropicAnalyzer(s,
          client: c, sleep: (_) async {}, normalizer: (b) async => b),
      wrapSuccess: (text) => jsonEncode({
        'content': [
          {'type': 'text', 'text': text}
        ]
      }),
      fx: {
        Scenario.auth: const Fx(
            401,
            '{"type": "error", "error": {"type": "authentication_error", '
            '"message": "invalid x-api-key"}}',
            expectSays: 'key'),
        Scenario.rateLimit: const Fx(
            429,
            '{"type": "error", "error": {"type": "rate_limit_error", '
            '"message": "Number of request tokens has exceeded your '
            'per-minute rate limit."}}',
            expectCalls: 1),
        // Anthropic reports an empty balance as HTTP 400, not 429 — the
        // scenario that motivated this matrix.
        Scenario.billing: const Fx(
            400,
            '{"type": "error", "error": {"type": "invalid_request_error", '
            '"message": "Your credit balance is too low to access the '
            'Anthropic API. Please go to Plans & Billing to upgrade or '
            'purchase credits."}}'),
        Scenario.modelNotFound: const Fx(
            404,
            '{"type": "error", "error": {"type": "not_found_error", '
            '"message": "model: claude-nope-1"}}'),
        Scenario.overloaded: const Fx(
            529,
            '{"type": "error", "error": {"type": "overloaded_error", '
            '"message": "Overloaded"}}',
            expectCalls: 3),
        Scenario.junkReply: const Fx(200, ''),
      },
    ),
    ProviderSpec(
      name: 'Server',
      settings: () => _settingsFor(AiProvider.server),
      make: (s, c) =>
          ServerAnalyzer(s, client: c, sleep: (_) async {}, normalizer: (b) async => b),
      supportsFencedReply: false, // the server pre-parses the CLI's reply
      wrapSuccess: (text) =>
          jsonEncode({'ok': true, 'analysis': jsonDecode(text)}),
      fx: {
        Scenario.auth: const Fx(401, '{"error": "Unauthorized"}',
            expectSays: 'key'),
        // CLI busy / analyzer off — the server's one "try later" signal.
        Scenario.overloaded: const Fx(
            503, '{"error": "claude_unavailable"}',
            expectCalls: 3),
        Scenario.junkReply: const Fx(200, '{"ok": true}'), // no analysis
      },
    ),
    ProviderSpec(
      name: 'Qwen',
      settings: () => _settingsFor(AiProvider.qwen),
      make: (s, c) => _compat(createQwenAnalyzer(s), s, c),
      wrapSuccess: _openaiShape,
      fx: {
        // Captured LIVE from dashscope.aliyuncs.com on 2026-07-30.
        Scenario.auth: const Fx(
            401,
            '{"error":{"message":"Incorrect API key provided. For details, '
            'see: https://help.aliyun.com/zh/model-studio/error-code'
            '#apikey-error","type":"invalid_request_error","param":null,'
            '"code":"invalid_api_key"},"request_id":"af287265"}',
            expectSays: 'key'),
        Scenario.rateLimit: const Fx(
            429,
            '{"error":{"code":"Throttling.RateQuota","message":"Requests '
            'rate limit exceeded, please try again later."}}',
            expectCalls: 1),
        Scenario.billing: const Fx(
            400,
            '{"error":{"code":"Arrearage","message":"Access denied, please '
            'make sure your account is in good standing."}}'),
        Scenario.modelNotFound: const Fx(
            404,
            '{"error":{"code":"InvalidParameter","message":"Model can not '
            'be found."}}',
            expectSays: 'model'),
        Scenario.overloaded: const Fx(
            500,
            '{"error":{"code":"InternalError","message":"Internal server '
            'error, please try again later."}}',
            expectCalls: 3),
        Scenario.junkReply: const Fx(200, ''),
      },
    ),
    ProviderSpec(
      name: 'Doubao',
      settings: () => _settingsFor(AiProvider.doubao),
      make: (s, c) => _compat(createDoubaoAnalyzer(s), s, c),
      wrapSuccess: _openaiShape,
      fx: {
        // Captured LIVE from ark.cn-beijing.volces.com on 2026-07-30.
        Scenario.auth: const Fx(
            401,
            '{"error":{"code":"AuthenticationError","message":"The API key '
            'format is incorrect. Request id: 0217853335425"'
            ',"param":"","type":"Unauthorized"}}',
            expectSays: 'key'),
        Scenario.rateLimit: const Fx(
            429,
            '{"error":{"code":"ModelAccountTpmRateLimitExceeded","message":'
            '"The Tokens Per Minute (TPM) limit of the associated model '
            'for your account has been exceeded."}}',
            expectCalls: 1),
        Scenario.billing: const Fx(
            403,
            '{"error":{"code":"AccountOverdueError","message":"The request '
            'failed because your account has an overdue balance. Request '
            'id: 02178"}}'),
        Scenario.modelNotFound: const Fx(
            404,
            '{"error":{"code":"InvalidEndpointOrModel.NotFound","message":'
            '"The model or endpoint doubao-seed-1-6 does not exist or you '
            'do not have access to it."}}',
            expectSays: '开通'),
        Scenario.overloaded: const Fx(
            429,
            '{"error":{"code":"ServerOverloaded","message":"The service is '
            'currently unable to handle additional requests due to server '
            'overload."}}',
            expectCalls: 1),
        Scenario.contentFilter: const Fx(
            400,
            '{"error":{"code":"SensitiveContentDetected","message":"The '
            'request failed because the input text may contain sensitive '
            'information."}}'),
        Scenario.junkReply: const Fx(200, ''),
      },
    ),
    ProviderSpec(
      name: 'GLM',
      settings: () => _settingsFor(AiProvider.glm),
      make: (s, c) => _compat(createGlmAnalyzer(s), s, c),
      wrapSuccess: _openaiShape,
      fx: {
        // Captured LIVE from open.bigmodel.cn on 2026-07-30.
        Scenario.auth: const Fx(
            401,
            '{"error":{"code":"1002","message":"Authorization Token非法，请确'
            '认Authorization Token正确传递。"}}',
            expectSays: 'key'),
        Scenario.rateLimit: const Fx(
            429,
            '{"error":{"code":"1302","message":"您当前使用该API的并发数过高，'
            '请降低并发，或联系客服增加限额。"}}',
            expectCalls: 1),
        Scenario.billing: const Fx(
            429,
            '{"error":{"code":"1113","message":"余额不足或无可用资源包，请充值'
            '后重试。"}}'),
        Scenario.modelNotFound: const Fx(
            400,
            '{"error":{"code":"1211","message":"模型不存在，请检查模型代码。"}}'),
        Scenario.overloaded: const Fx(
            429,
            '{"error":{"code":"1305","message":"当前API请求过多，请稍后重试。"}}',
            expectCalls: 1),
        Scenario.junkReply: const Fx(200, ''),
      },
    ),
  ];

  for (final p in providers) {
    group('${p.name} feedback', () {
      test('success: clean JSON reply becomes a saved analysis', () async {
        final s = await p.settings();
        final a = p.make(
            s,
            MockClient((_) async => http.Response(
                p.wrapSuccess(_goodJson), 200,
                headers: _utf8Json)));
        final out = await a.analyzePhoto(photo);
        expect(out.isFood, isTrue, reason: p.name);
        expect(out.analysis!['total_calories'], 620, reason: p.name);
      });

      if (p.supportsFencedReply) {
        test('success: markdown-fenced JSON still parses', () async {
          final s = await p.settings();
          final a = p.make(
              s,
              MockClient((_) async => http.Response(
                  p.wrapSuccess('```json\n$_goodJson\n```'), 200,
                  headers: _utf8Json)));
          final out = await a.analyzePhoto(photo);
          expect(out.isFood, isTrue, reason: p.name);
        });
      }

      test('success: a not-food verdict is preserved, not an error',
          () async {
        final s = await p.settings();
        final notFood =
            '{"is_food": false, "reason": "order screenshot of socks"}';
        final a = p.make(
            s,
            MockClient((_) async => http.Response(
                p.wrapSuccess(notFood), 200,
                headers: _utf8Json)));
        final out = await a.analyzePhoto(photo);
        expect(out.isFood, isFalse, reason: p.name);
        expect(out.error, isNull, reason: p.name);
        expect(out.analysis, isNotNull, reason: p.name);
      });

      for (final entry in p.fx.entries) {
        final scenario = entry.key;
        final fx = entry.value;
        test('${scenario.name}: HTTP ${fx.status} classifies correctly',
            () async {
          final s = await p.settings();
          var calls = 0;
          final body = scenario == Scenario.junkReply && fx.body.isEmpty
              ? p.wrapSuccess(
                  "Sorry, I can't help with identifying food today.")
              : fx.body;
          final a = p.make(s, MockClient((_) async {
            calls++;
            return http.Response(body, fx.status, headers: _utf8Json);
          }));
          final out = await a.analyzePhoto(photo);
          final why = '${p.name}/${scenario.name}: ${out.error}';

          switch (scenario) {
            case Scenario.auth:
              expect(out.retryable, isFalse, reason: why);
              expect(out.error!.toLowerCase(), contains('key'), reason: why);
            case Scenario.rateLimit:
              expect(out.retryable, isTrue, reason: why);
              expect(s.isQuotaPaused, isFalse,
                  reason: '$why — a minute-scale rate blip must never arm '
                      'the DAILY pause latch');
            case Scenario.billing:
              expect(out.retryable, isTrue,
                  reason: '$why — a recharge fixes it; the photo must '
                      'survive, not burn');
              expect(out.error!.toLowerCase(), contains('billing'),
                  reason: why);
              expect(s.isQuotaPaused, isFalse, reason: why);
            case Scenario.modelNotFound:
              expect(out.retryable, isFalse,
                  reason: '$why — retrying a nonexistent model loops '
                      'forever');
            case Scenario.overloaded:
              expect(out.retryable, isTrue, reason: why);
              expect(s.isQuotaPaused, isFalse, reason: why);
            case Scenario.contentFilter:
              expect(out.retryable, isFalse, reason: why);
            case Scenario.junkReply:
              expect(out.analysis, isNull, reason: why);
              expect(out.retryable, isFalse,
                  reason: '$why — the model ANSWERED; a retry re-spends '
                      'for the same junk');
            case Scenario.dailyQuota:
              expect(out.retryable, isTrue, reason: why);
              expect(s.isQuotaPaused, isTrue,
                  reason: '$why — the Gemini free-tier daily cap is the '
                      'one signal that must arm the persisted latch');
          }
          if (fx.expectSays != null) {
            expect(out.error, contains(fx.expectSays!), reason: why);
          }
          if (fx.expectCalls != null) {
            expect(calls, fx.expectCalls, reason: why);
          }
        });
      }
    });
  }
}
