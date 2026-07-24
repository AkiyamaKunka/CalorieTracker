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
  static const int maxOriginalFallbackBytes = 5 * 1024 * 1024; // spec §3.1

  /// Provider hooks.
  String? get apiKey;
  String get model;
  http.Request buildRequest(String key,
      {required String prompt, Uint8List? jpegBytes, required int maxTokens});
  String extractText(Map<String, dynamic> body);

  /// One POST. Returns the reply text; throws [_ProviderException] with a
  /// transient/permanent classification otherwise.
  Future<String> _post(
      {required String prompt,
      Uint8List? jpegBytes,
      String? apiKeyOverride,
      int maxTokens = 2048}) async {
    final key = (apiKeyOverride ?? apiKey ?? '').trim();
    if (key.isEmpty) {
      throw const _ProviderException('No API key configured for this provider',
          transient: true); // user state, not a photo defect
    }
    final req = buildRequest(key,
        prompt: prompt, jpegBytes: jpegBytes, maxTokens: maxTokens);
    http.StreamedResponse streamed;
    try {
      streamed = await client.send(req).timeout(httpDeadline);
    } on TimeoutException {
      throw const _ProviderException('client deadline hit', transient: true);
    } catch (e) {
      throw _ProviderException('connection error: $e', transient: true);
    }
    final resp = await http.Response.fromStream(streamed);
    if (resp.statusCode == 401 || resp.statusCode == 403) {
      throw const _ProviderException('key rejected',
          transient: false, auth: true);
    }
    if (resp.statusCode != 200) {
      throw _ProviderException('HTTP ${resp.statusCode}',
          transient: _isTransientStatus(resp.statusCode),
          quotaClass: resp.statusCode == 429);
    }
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
        if (attempt < maxAttempts && e.transient && !e.quotaClass) {
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
      await _post(prompt: 'ping', apiKeyOverride: apiKey, maxTokens: 1);
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
      {required this.transient, this.auth = false, this.quotaClass = false});
  final String message;
  final bool transient;
  final bool auth;
  final bool quotaClass;

  String get userMessage {
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
        'max_tokens': maxTokens,
      });
    return req;
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
  String extractText(Map<String, dynamic> body) =>
      ((body['content'] as List).first as Map)['text'] as String;
}

/// Per-call delegation on [AppSettings.provider]: switching providers in
/// Settings takes effect on the NEXT request — no DI rewiring, no restart.
class MultiProviderAnalyzer implements AnalyzerService {
  MultiProviderAnalyzer(this._settings, {http.Client? client})
      : _gemini = createAnalyzer(_settings, client: client),
        _openai = OpenAiAnalyzer(_settings, client: client),
        _anthropic = AnthropicAnalyzer(_settings, client: client);

  final AppSettings _settings;
  final AnalyzerService _gemini;
  final OpenAiAnalyzer _openai;
  final AnthropicAnalyzer _anthropic;

  AnalyzerService get _active => switch (_settings.provider) {
        AiProvider.gemini => _gemini,
        AiProvider.openai => _openai,
        AiProvider.anthropic => _anthropic,
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
