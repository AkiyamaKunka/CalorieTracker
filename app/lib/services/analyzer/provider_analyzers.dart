/// OpenAI + Anthropic vision backends behind the same [AnalyzerService]
/// seam as Gemini, plus the per-call delegating switch.
///
/// Design: GeminiAnalyzer is spec-pinned and untouched; these mirror its
/// behavioral contract (normalize → prompt(+dietary) → bounded retries on
/// transient classes → parseAiJson → coerceIsFood; validateKey = 1-token
/// real generation where quota-class responses count as ACCEPTED because
/// they prove the key authenticated). The shared prompts from shared/ are
/// provider-agnostic text, so server↔app parity is unaffected — the golden
/// vectors guard everything downstream of the model reply.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/foundation.dart' show compute;
import 'package:http/http.dart' as http;

import '../../core/coerce.dart';
import '../../core/contracts.dart';
import '../../core/prompts.dart';
import '../settings/app_settings.dart';
import 'gemini_analyzer.dart' show createAnalyzer;
import 'normalize.dart';

/// Transient failure classes shared by both providers: HTTP 429 (rate or
/// spend limits), 5xx/overloaded, client deadline, transport errors.
bool _isTransientStatus(int status) =>
    status == 429 || status == 529 || (status >= 500 && status < 600);

Future<Uint8List?> _computeNormalize(Uint8List bytes) =>
    compute(normalizeForAnalysis, bytes);

/// Common skeleton: subclasses supply the endpoint request and reply-text
/// extraction; everything else (normalize, retries, coercion, validate
/// semantics) is identical across providers.
abstract class _HttpVisionAnalyzer implements AnalyzerService {
  _HttpVisionAnalyzer(
    this.settings, {
    http.Client? client,
    Future<void> Function(Duration)? sleep,
    Future<Uint8List?> Function(Uint8List)? normalizer,
  })  : client = client ?? http.Client(),
        _sleep = sleep ?? ((d) => Future<void>.delayed(d)),
        _normalize = normalizer ?? _computeNormalize;

  final AppSettings settings;
  final http.Client client;
  final Future<void> Function(Duration) _sleep;
  final Future<Uint8List?> Function(Uint8List) _normalize;

  static const int maxAttempts = 3; // parity with spec §3.3
  static const Duration httpDeadline = Duration(seconds: 90); // spec §3.2

  /// Per-provider wall clock. Direct model APIs keep the §3.2 90 s; a
  /// provider fronting slower work (a CLI run on the user's server) must
  /// allow more, or the client gives up while the server is still working
  /// and the retry only meets its own busy lock.
  Duration get deadline => httpDeadline;
  static const int maxOriginalFallbackBytes = 5 * 1024 * 1024; // spec §3.1

  /// Provider hooks.
  String? get apiKey;
  String get model;
  String get providerLabel;

  /// Verbatim message for an HTTP 404, or null for the generic bucket.
  /// The compat providers override this: a 404 there almost always means
  /// a wrong / retired / not-yet-activated MODEL id, not a bad key — and
  /// the generic message left users regenerating a key that worked.
  String? get notFoundMessage => null;
  http.Request buildRequest(String key,
      {required String prompt, Uint8List? jpegBytes, required int maxTokens});
  String extractText(Map<String, dynamic> body);

  /// One POST. Returns the reply text; throws [_ProviderException] with a
  /// transient/permanent classification otherwise. [extract] false skips
  /// text extraction entirely (validateKey: an HTTP 200 proves the key
  /// regardless of reply shape — a 1-token reply may contain no text
  /// block at all, same rule as Gemini's MAX_TOKENS case).
  Future<String> _post(
      {required String prompt,
      Uint8List? jpegBytes,
      String? apiKeyOverride,
      int maxTokens = 2048,
      bool extract = true}) async {
    final key = (apiKeyOverride ?? apiKey ?? '').trim();
    if (key.isEmpty) {
      // User state, not a photo defect: the photo stays eligible
      // (retryable) but re-attempting in place is pointless.
      throw _ProviderException(
          'No $providerLabel API key — add one in Settings.',
          transient: true,
          retryInPlace: false,
          verbatimMessage: true);
    }
    final req = buildRequest(key,
        prompt: prompt, jpegBytes: jpegBytes, maxTokens: maxTokens);
    http.StreamedResponse streamed;
    try {
      streamed = await client.send(req).timeout(deadline);
    } on TimeoutException {
      throw const _ProviderException('client deadline hit', transient: true);
    } catch (e) {
      throw _ProviderException('connection error: $e', transient: true);
    }
    final resp = await http.Response.fromStream(streamed);
    if (resp.statusCode != 200) {
      // Exhausted-billing signatures, checked BEFORE the auth
      // classification because the providers disagree on the status code:
      //   OpenAI    429 insufficient_quota
      //   Zhipu GLM 429 code "1113" (余额不足 — also NOT a rate limit)
      //   Ark/Doubao 403 AccountOverdueError — a 403 that is NOT auth;
      //     treating it as "key rejected" burned the photo non-retryably
      //     and told the user to fix a key that works (review 2026-07-29)
      //   DashScope 400 Arrearage — a 400 that is NOT a client bug
      // All mean "the key authenticated but the account needs money":
      // transient (the photo stays eligible; a recharge fixes it) and
      // quota-classed (validateKey accepts — the key itself is proven).
      final body = resp.body;
      final billingDead = (resp.statusCode == 429 &&
              (body.contains('insufficient_quota') ||
                  body.contains('"1113"'))) ||
          (resp.statusCode == 403 && body.contains('AccountOverdue')) ||
          (resp.statusCode == 400 && body.contains('Arrearage'));
      if (billingDead) {
        throw _ProviderException(
            'Provider balance or credits exhausted — check your '
            '$providerLabel billing.',
            transient: true,
            quotaClass: true,
            verbatimMessage: true);
      }
      if (resp.statusCode == 401 || resp.statusCode == 403) {
        throw const _ProviderException('key rejected',
            transient: false, auth: true);
      }
      final notFound = notFoundMessage;
      if (resp.statusCode == 404 && notFound != null) {
        throw _ProviderException(notFound,
            transient: false, verbatimMessage: true);
      }
      throw _ProviderException('HTTP ${resp.statusCode}',
          transient: _isTransientStatus(resp.statusCode),
          quotaClass: resp.statusCode == 429);
    }
    if (!extract) return '';
    try {
      return extractText(jsonDecode(resp.body) as Map<String, dynamic>);
    } catch (_) {
      throw const _ProviderException('unexpected response shape',
          transient: false);
    }
  }

  @override
  Future<AnalysisOutcome> analyzePhoto(Uint8List originalBytes) async {
    final sw = Stopwatch()..start();
    if (settings.isQuotaPaused) {
      return AnalysisOutcome(
          error: 'Analysis paused (quota) — retrying later.',
          retryable: true,
          wall: sw.elapsed);
    }
    Uint8List? sendBytes = await _normalize(originalBytes);
    if (sendBytes == null) {
      if (originalBytes.length < maxOriginalFallbackBytes) {
        sendBytes = originalBytes;
      } else {
        return AnalysisOutcome(
            error: 'Could not process this photo (decode failed and it is '
                'too large to send unprocessed).',
            wall: sw.elapsed);
      }
    }
    final prompt =
        withDietaryProfile(foodDetectionPrompt, settings.dietaryProfile);
    for (var attempt = 1;; attempt++) {
      String text;
      try {
        text = await _post(prompt: prompt, jpegBytes: sendBytes);
      } on _ProviderException catch (e) {
        if (attempt < maxAttempts &&
            e.transient &&
            e.retryInPlace &&
            !e.quotaClass) {
          await _sleep(Duration(seconds: 5 * attempt));
          continue;
        }
        return AnalysisOutcome(
            error: e.userMessage, retryable: e.transient, wall: sw.elapsed);
      }
      dynamic parsed;
      try {
        parsed = parseAiJson(text);
      } on FormatException {
        return AnalysisOutcome(
            error: "Couldn't understand the AI response.", wall: sw.elapsed);
      }
      if (parsed is! Map) {
        return AnalysisOutcome(
            error: 'The AI returned an unusable analysis.', wall: sw.elapsed);
      }
      final analysis = Map<String, dynamic>.from(parsed);
      final isFood = coerceIsFood(analysis);
      if (isFood == null) {
        return AnalysisOutcome(
            error: 'The AI returned an unusable analysis.', wall: sw.elapsed);
      }
      analysis['is_food'] = isFood;
      return AnalysisOutcome(
          analysis: analysis, isFood: isFood, wall: sw.elapsed);
    }
  }

  @override
  Future<Map<String, dynamic>?> textIntent(String prompt) async {
    String text;
    try {
      text = await _post(prompt: prompt);
    } on _ProviderException {
      return null;
    }
    dynamic parsed;
    try {
      parsed = parseAiJson(text);
    } on FormatException {
      return null;
    }
    if (parsed is Map) return Map<String, dynamic>.from(parsed);
    if (parsed is List) return {'actions': parsed}; // §4.1 bare-array rule
    return null;
  }

  @override
  Future<String?> validateKey(String apiKey) async {
    try {
      // extract:false — HTTP 200 alone proves the key; a 1-token reply may
      // legitimately carry no text block (Gemini MAX_TOKENS parity).
      await _post(
          prompt: 'ping', apiKeyOverride: apiKey, maxTokens: 1, extract: false);
      return null;
    } on _ProviderException catch (e) {
      // Same rule as Gemini: a REAL HTTP quota-class response proves the
      // key authenticated → accepted. Auth/permanent → error message.
      if (e.quotaClass) return null;
      return e.userMessage;
    }
  }
}

class _ProviderException implements Exception {
  const _ProviderException(this.message,
      {required this.transient,
      this.auth = false,
      this.quotaClass = false,
      this.retryInPlace = true,
      this.verbatimMessage = false});
  final String message;
  final bool transient;
  final bool auth;
  final bool quotaClass;

  /// False for conditions that cannot change within this call (missing
  /// key): still retryable at the OUTCOME level, but in-place sleeps are
  /// pointless.
  final bool retryInPlace;

  /// True when [message] is already user-facing (skip the generic bucket).
  final bool verbatimMessage;

  String get userMessage {
    if (verbatimMessage) return message;
    if (auth) return 'The provider rejected the API key. Check Settings.';
    if (quotaClass) {
      return 'Provider rate/spend limit hit — will retry later.';
    }
    if (transient) {
      return 'Error contacting the provider (network or service issue).';
    }
    return 'Provider request failed: $message';
  }
}

/// OpenAI chat-completions vision backend.
class OpenAiAnalyzer extends _HttpVisionAnalyzer {
  OpenAiAnalyzer(super.settings,
      {super.client, super.sleep, super.normalizer});

  static final Uri _endpoint =
      Uri.parse('https://api.openai.com/v1/chat/completions');

  @override
  String? get apiKey => settings.openaiApiKey;
  @override
  String get model => settings.openaiModel;
  @override
  String get providerLabel => 'OpenAI';

  @override
  http.Request buildRequest(String key,
      {required String prompt, Uint8List? jpegBytes, required int maxTokens}) {
    final content = <Map<String, Object?>>[
      {'type': 'text', 'text': prompt},
      if (jpegBytes != null)
        {
          'type': 'image_url',
          'image_url': {
            'url': 'data:image/jpeg;base64,${base64Encode(jpegBytes)}'
          }
        },
    ];
    final req = http.Request('POST', _endpoint)
      ..headers['Authorization'] = 'Bearer $key'
      ..headers['Content-Type'] = 'application/json'
      ..body = jsonEncode({
        'model': model,
        'messages': [
          {'role': 'user', 'content': content}
        ],
        // Our shared prompts already instruct JSON; json_object mode makes
        // the syntax guarantee explicit (schema stays the coercers' job).
        if (jpegBytes != null) 'response_format': {'type': 'json_object'},
        // NOT 'max_tokens': newer models (o-series, gpt-5 family) reject it
        // with 400; max_completion_tokens is accepted across current models.
        'max_completion_tokens': maxTokens,
      });
    return req;
  }

  @override
  String extractText(Map<String, dynamic> body) =>
      ((body['choices'] as List).first as Map)['message']['content'] as String;
}

/// Any OpenAI-compatible chat-completions vision backend, parameterized by
/// endpoint. This is how the mainland-China providers (Alibaba Qwen,
/// ByteDance Doubao, Zhipu GLM — all of which expose OpenAI-compatible
/// APIs) ride the exact request/retry/coercion contract OpenAI already
/// gets, without three copies of the class.
///
/// Differences from [OpenAiAnalyzer] kept deliberately:
/// - `max_tokens`, not `max_completion_tokens`: the compat layers accept
///   the classic field; several reject the newer one with 400.
/// - `response_format` json_object only when [supportsJsonMode] — Zhipu's
///   API reference marks the field TEXT-models-only ("仅文本模型支持此字段"),
///   so vision calls there must rely on the prompt + fence-stripping parse.
/// - [extraBody] for provider extension fields — Doubao and GLM vision
///   models THINK by default; `thinking: {type: disabled}` cuts the cost
///   and latency of a fixed-schema extraction task (researched 2026-07-29).
class OpenAiCompatAnalyzer extends _HttpVisionAnalyzer {
  OpenAiCompatAnalyzer(
    super.settings, {
    required this.endpoint,
    required this.label,
    required this.keyOf,
    required this.modelOf,
    this.supportsJsonMode = true,
    this.extraBody = const {},
    this.notFoundHint,
    super.client,
    super.sleep,
    super.normalizer,
  });

  final Uri endpoint;
  final String label;
  final String? Function(AppSettings) keyOf;
  final String Function(AppSettings) modelOf;
  final bool supportsJsonMode;
  final Map<String, Object?> extraBody;

  /// Per-provider HTTP-404 explanation (see [notFoundMessage]).
  final String? notFoundHint;

  @override
  String? get apiKey => keyOf(settings);
  @override
  String get model => modelOf(settings);
  @override
  String get providerLabel => label;
  @override
  String? get notFoundMessage => notFoundHint;

  @override
  http.Request buildRequest(String key,
      {required String prompt, Uint8List? jpegBytes, required int maxTokens}) {
    final content = <Map<String, Object?>>[
      {'type': 'text', 'text': prompt},
      if (jpegBytes != null)
        {
          'type': 'image_url',
          'image_url': {
            'url': 'data:image/jpeg;base64,${base64Encode(jpegBytes)}'
          }
        },
    ];
    // The model field is free text: a user-typed *thinking* model id
    // (doubao-seed-*-thinking-*, glm-4.1v-thinking-*) REQUIRES thinking,
    // and sending {type: disabled} to one is a hard 400 on every call.
    final extras = Map<String, Object?>.of(extraBody);
    if (model.contains('thinking')) extras.remove('thinking');
    return http.Request('POST', endpoint)
      ..headers['Authorization'] = 'Bearer $key'
      ..headers['Content-Type'] = 'application/json'
      // extras spread FIRST: on a key collision the computed core fields
      // must win, or a config entry could silently clobber model/messages
      // or validateKey's 1-token cap (jsonEncode keeps the LAST value —
      // no error would ever surface).
      ..body = jsonEncode({
        ...extras,
        'model': model,
        'messages': [
          {'role': 'user', 'content': content}
        ],
        if (supportsJsonMode && jpegBytes != null)
          'response_format': {'type': 'json_object'},
        'max_tokens': maxTokens,
      });
  }

  @override
  String extractText(Map<String, dynamic> body) =>
      ((body['choices'] as List).first as Map)['message']['content'] as String;
}

/// Anthropic messages-API vision backend.
class AnthropicAnalyzer extends _HttpVisionAnalyzer {
  AnthropicAnalyzer(super.settings,
      {super.client, super.sleep, super.normalizer});

  static final Uri _endpoint = Uri.parse('https://api.anthropic.com/v1/messages');

  @override
  String? get apiKey => settings.anthropicApiKey;
  @override
  String get model => settings.anthropicModel;
  @override
  String get providerLabel => 'Anthropic';

  @override
  http.Request buildRequest(String key,
      {required String prompt, Uint8List? jpegBytes, required int maxTokens}) {
    final content = <Map<String, Object?>>[
      if (jpegBytes != null)
        {
          'type': 'image',
          'source': {
            'type': 'base64',
            'media_type': 'image/jpeg',
            'data': base64Encode(jpegBytes),
          }
        },
      {'type': 'text', 'text': prompt},
    ];
    final req = http.Request('POST', _endpoint)
      ..headers['x-api-key'] = key
      ..headers['anthropic-version'] = '2023-06-01'
      ..headers['Content-Type'] = 'application/json'
      ..body = jsonEncode({
        'model': model,
        'max_tokens': maxTokens,
        'messages': [
          {'role': 'user', 'content': content}
        ],
      });
    return req;
  }

  @override
  String extractText(Map<String, dynamic> body) {
    // Content may lead with thinking blocks (claude-sonnet-5's adaptive
    // thinking) — the answer is the FIRST TEXT block, not content.first.
    for (final block in body['content'] as List) {
      if (block is Map && block['type'] == 'text') {
        return block['text'] as String;
      }
    }
    throw const FormatException('no text block in reply');
  }
}

/// Per-call delegation on [AppSettings.provider]: switching providers in
/// Settings takes effect on the NEXT request — no DI rewiring, no restart.
/// The user's OWN CalorieTracker server as an analyzer: photos and prompts
/// go to its /api/analyze_photo and /api/text_intent endpoints, which run
/// the Claude Code CLI under the owner's Claude SUBSCRIPTION — so analyses
/// cost no API money.
///
/// Why the phone does NOT carry a subscription token directly: Claude
/// subscription credentials authorize Claude Code itself, not arbitrary
/// third-party clients, and an APK is a shipping container for whatever is
/// baked into it (this build gets handed to other people). The server
/// already holds that credential legitimately and runs the real CLI, so the
/// device only ever stores the server's own upload key.
///
/// Reachability caveat worth knowing: this points at a plain-HTTP endpoint
/// on the user's VM, so the upload key and photo bytes cross the network
/// unencrypted — same exposure the existing Termux watcher already accepts.
/// TLS in front of Flask is the fix; the app is ready for an https:// base
/// URL the moment the server has one.
class ServerAnalyzer extends _HttpVisionAnalyzer {
  ServerAnalyzer(super.settings, {super.client, super.sleep, super.normalizer});

  @override
  String? get apiKey => settings.serverApiKey;
  @override
  String get model => 'server';
  @override
  String get providerLabel => 'server';

  /// The server runs a Claude CLI analysis (up to ~120 s, and up to ~240 s
  /// across its internal stream→file fallback) behind a synchronous Flask
  /// handler, so the 90 s API default would abandon work that is still
  /// running — and the retry would only meet the server's own
  /// single-flight lock, turning a slow success into a hard failure.
  @override
  Duration get deadline => const Duration(minutes: 5);

  Uri _uri(String path) => Uri.parse('${settings.serverBaseUrl}$path');

  @override
  http.Request buildRequest(String key,
      {required String prompt, Uint8List? jpegBytes, required int maxTokens}) {
    if (settings.serverBaseUrl.isEmpty) {
      // Retryable at the OUTCOME level (the user may add the address later)
      // but nothing to retry in place.
      throw const _ProviderException(
          'Add your server address in Settings to use the subscription path.',
          transient: true,
          retryInPlace: false,
          verbatimMessage: true);
    }
    final isPhoto = jpegBytes != null;
    return http.Request(
        'POST', _uri(isPhoto ? '/api/analyze_photo' : '/api/text_intent'))
      ..headers['content-type'] = 'application/json'
      ..headers['X-API-Key'] = key
      ..headers['X-Client-Platform'] = 'app'
      // PHOTO: send only the dietary PROFILE, never a whole prompt. The
      // server composes the analysis prompt from its own shared/ copy — a
      // caller-authored prompt is an instruction channel into the CLI, and
      // the CLI's image path necessarily has the Read tool enabled.
      // TEXT: the prompt IS the user's request, and that endpoint runs
      // with tools disabled.
      ..body = jsonEncode(isPhoto
          ? {
              'image_b64': base64Encode(jpegBytes),
              'dietary_profile': settings.dietaryProfile ?? '',
            }
          : {'prompt': prompt});
  }

  @override
  String extractText(Map<String, dynamic> body) {
    // Photos answer {ok, analysis}, text answers {ok, result}. The base
    // layer wants TEXT to parseAiJson, so hand the payload back as JSON —
    // that keeps every downstream rule (coerceIsFood, bare-array handling)
    // identical to the direct-API providers.
    final payload = body['analysis'] ?? body['result'];
    if (payload == null) throw const FormatException('no payload');
    return jsonEncode(payload);
  }

  /// Begin the server's Claude OAuth re-connect (the server spawns its own
  /// `claude setup-token`): returns the OFFICIAL Anthropic authorize URL
  /// for the system browser, or an error message. No credential is
  /// involved on this side beyond the server upload key.
  Future<({String? url, String? error})> startClaudeAuth() async {
    final base = settings.serverBaseUrl;
    final key = (settings.serverApiKey ?? '').trim();
    if (base.isEmpty || key.isEmpty) {
      return (url: null, error: 'Set the server address and key first.');
    }
    try {
      final req = http.Request('POST', _uri('/api/claude_auth/start'))
        ..headers['content-type'] = 'application/json'
        ..headers['X-API-Key'] = key
        ..body = '{}';
      final resp = await http.Response.fromStream(
          await client.send(req).timeout(deadline));
      final body = jsonDecode(resp.body) as Map<String, dynamic>;
      if (resp.statusCode == 200 && body['url'] is String) {
        return (url: body['url'] as String, error: null);
      }
      return (
        url: null,
        error: (body['reason'] ?? 'Server answered HTTP ${resp.statusCode}.')
            .toString()
      );
    } catch (e) {
      return (url: null, error: 'Could not reach the server.');
    }
  }

  /// Hand the pasted authorization code to the waiting server flow.
  /// Null = connected; otherwise a user-facing error.
  Future<String?> completeClaudeAuth(String code) async {
    final key = (settings.serverApiKey ?? '').trim();
    try {
      final req = http.Request('POST', _uri('/api/claude_auth/complete'))
        ..headers['content-type'] = 'application/json'
        ..headers['X-API-Key'] = key
        ..body = jsonEncode({'code': code.trim()});
      final resp = await http.Response.fromStream(
          await client.send(req).timeout(deadline));
      if (resp.statusCode == 200) return null;
      final body = jsonDecode(resp.body) as Map<String, dynamic>;
      return (body['reason'] ?? 'Server answered HTTP ${resp.statusCode}.')
          .toString();
    } catch (e) {
      return 'Could not reach the server.';
    }
  }

  /// /api/auth_check proves "address reachable + key accepted" with no side
  /// effects and no CLI run. Deliberately NOT /ping: that is the Termux
  /// watcher's liveness channel, and stamping it from here would forge
  /// watcher liveness and mute the stale-watcher outage alert.
  @override
  Future<String?> validateKey(String apiKey) async {
    final base = settings.serverBaseUrl;
    if (base.isEmpty) return 'Add your server address first.';
    final key = apiKey.trim();
    if (key.isEmpty) return 'Enter your server upload key.';
    final req = http.Request('POST', _uri('/api/auth_check'))
      ..headers['content-type'] = 'application/json'
      ..headers['X-API-Key'] = key
      ..headers['X-Client-Platform'] = 'app'
      ..body = '{}';
    try {
      final streamed = await client.send(req).timeout(deadline);
      final resp = await http.Response.fromStream(streamed);
      if (resp.statusCode == 200) return null;
      if (resp.statusCode == 401 || resp.statusCode == 403) {
        return 'Your server rejected that key.';
      }
      return 'Server answered HTTP ${resp.statusCode}.';
    } on TimeoutException {
      return 'Server did not respond (timeout).';
    } catch (e) {
      return 'Could not reach $base.';
    }
  }
}

/// The mainland-China providers, as [OpenAiCompatAnalyzer] configurations.
/// Endpoints are the providers' OpenAI-compatible chat-completions URLs —
/// mainland regions, reachable there without a VPN. Facts verified against
/// current provider docs 2026-07-29 (see the workflow research notes).
OpenAiCompatAnalyzer createQwenAnalyzer(AppSettings settings,
        {http.Client? client}) =>
    OpenAiCompatAnalyzer(settings,
        // The MAINLAND (Beijing) platform. dashscope-intl (Singapore) is a
        // separate platform with separate keys — a mainland key gets 401
        // there. This app targets mainland users; intl users have the
        // other four providers.
        endpoint: Uri.parse(
            'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'),
        label: 'Qwen',
        keyOf: (s) => s.qwenApiKey,
        modelOf: (s) => s.qwenModel,
        // json_object works on the stable qwen3-vl/qwen-vl IDs and needs
        // the word "json" in a message — both shared prompts say JSON.
        // qwen3-vl defaults to thinking OFF in compat mode: send nothing.
        notFoundHint: 'Qwen model not found — check the model name in '
            'Settings (stable IDs like qwen3-vl-flash; -latest snapshots '
            'lack JSON mode).',
        client: client);

OpenAiCompatAnalyzer createDoubaoAnalyzer(AppSettings settings,
        {http.Client? client}) =>
    OpenAiCompatAnalyzer(settings,
        endpoint: Uri.parse(
            'https://ark.cn-beijing.volces.com/api/v3/chat/completions'),
        label: 'Doubao',
        keyOf: (s) => s.doubaoApiKey,
        modelOf: (s) => s.doubaoModel,
        // Seed models deep-think by default and bill the reasoning as
        // output tokens — pointless for a fixed-schema extraction.
        extraBody: const {
          'thinking': {'type': 'disabled'}
        },
        // Ark 404s both wrong ids AND not-yet-activated models; the
        // versioned-ID rule makes this the most common Doubao failure.
        notFoundHint: 'Doubao model not found — Ark needs the EXACT '
            'versioned ID from its model list, and the model must be '
            'activated (开通管理) in the Volcengine console.',
        client: client);

OpenAiCompatAnalyzer createGlmAnalyzer(AppSettings settings,
        {http.Client? client}) =>
    OpenAiCompatAnalyzer(settings,
        endpoint: Uri.parse(
            'https://open.bigmodel.cn/api/paas/v4/chat/completions'),
        label: 'GLM',
        keyOf: (s) => s.glmApiKey,
        modelOf: (s) => s.glmModel,
        // Zhipu's reference marks response_format TEXT-models-only; a
        // vision call must not send it (prompt + fence-stripping parse
        // carry the JSON contract, same as Anthropic).
        supportsJsonMode: false,
        extraBody: const {
          'thinking': {'type': 'disabled'}
        },
        notFoundHint: 'GLM model not found — check the model name in '
            'Settings (glm-4.6v family; older glm-4v IDs are retired).',
        client: client);

class MultiProviderAnalyzer implements AnalyzerService {
  MultiProviderAnalyzer(this._settings, {http.Client? client})
      : _gemini = createAnalyzer(_settings, client: client),
        _openai = OpenAiAnalyzer(_settings, client: client),
        _anthropic = AnthropicAnalyzer(_settings, client: client),
        _server = ServerAnalyzer(_settings, client: client),
        _qwen = createQwenAnalyzer(_settings, client: client),
        _doubao = createDoubaoAnalyzer(_settings, client: client),
        _glm = createGlmAnalyzer(_settings, client: client);

  final AppSettings _settings;
  final AnalyzerService _gemini;
  final OpenAiAnalyzer _openai;
  final AnthropicAnalyzer _anthropic;
  final ServerAnalyzer _server;
  final OpenAiCompatAnalyzer _qwen;
  final OpenAiCompatAnalyzer _doubao;
  final OpenAiCompatAnalyzer _glm;

  AnalyzerService get _active => switch (_settings.provider) {
        AiProvider.gemini => _gemini,
        AiProvider.openai => _openai,
        AiProvider.anthropic => _anthropic,
        AiProvider.server => _server,
        AiProvider.qwen => _qwen,
        AiProvider.doubao => _doubao,
        AiProvider.glm => _glm,
      };

  @override
  Future<AnalysisOutcome> analyzePhoto(Uint8List originalBytes) =>
      _active.analyzePhoto(originalBytes);

  @override
  Future<Map<String, dynamic>?> textIntent(String prompt) =>
      _active.textIntent(prompt);

  @override
  Future<String?> validateKey(String apiKey) => _active.validateKey(apiKey);
}

/// Integration seam for di.dart — replaces the direct Gemini factory.
AnalyzerService createMultiProviderAnalyzer(AppSettings settings,
        {http.Client? client}) =>
    MultiProviderAnalyzer(settings, client: client);
