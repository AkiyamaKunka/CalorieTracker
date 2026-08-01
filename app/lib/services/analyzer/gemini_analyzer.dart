/// Gemini-backed [AnalyzerService] (spec §3): REST generateContent with the
/// user's own key, JSON response mode, retry/backoff classification, and the
/// persisted daily-quota pause latch.
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
import 'normalize.dart';

/// Frozen integration seam.
AnalyzerService createAnalyzer(AppSettings settings, {http.Client? client}) =>
    GeminiAnalyzer(settings, client: client);

/// Internal transport/API failure carrying the raw error text that the
/// classifier (port of `_classify_gemini_error`) matches against.
class GeminiException implements Exception {
  const GeminiException(this.message);
  final String message;
  @override
  String toString() => 'GeminiException: $message';
}

/// Error classes, port of `_classify_gemini_error` (spec §3.3).
enum GeminiErrorClass {
  dailyQuotaExhausted,
  quotaRateLimit,
  auth,
  networkService,
  modelError,
  unknown,
}

/// Port of `_is_daily_free_tier_quota_error` (spec §3.3): the free-tier
/// per-day quota strings, matched on the upper-cased error text.
bool isDailyFreeTierQuotaError(String errorText) {
  final up = errorText.toUpperCase();
  if (up.contains('GENERATEREQUESTSPERDAYPERPROJECTPERMODEL-FREETIER')) {
    return true;
  }
  return up.contains('GENERATE_CONTENT_FREE_TIER_REQUESTS') &&
      (up.contains('PERDAY') || up.contains('PER DAY') || up.contains('LIMIT: 20'));
}

/// Spec §3.3 classification order: daily quota first (its text also contains
/// QUOTA/429), then rate limit, auth, network, model, unknown.
GeminiErrorClass classifyGeminiError(String errorText) {
  final up = errorText.toUpperCase();
  if (isDailyFreeTierQuotaError(up)) return GeminiErrorClass.dailyQuotaExhausted;
  if (up.contains('RESOURCE_EXHAUSTED') ||
      up.contains('429') ||
      up.contains('QUOTA') ||
      up.contains('RATE_LIMIT')) {
    return GeminiErrorClass.quotaRateLimit;
  }
  if (up.contains('API_KEY') ||
      up.contains('UNAUTHENTICATED') ||
      up.contains('PERMISSION_DENIED') ||
      up.contains('401') ||
      up.contains('403')) {
    return GeminiErrorClass.auth;
  }
  if (up.contains('UNAVAILABLE') ||
      up.contains('DEADLINE') ||
      up.contains('TIMEOUT') ||
      up.contains('CONNECTION') ||
      up.contains('503')) {
    return GeminiErrorClass.networkService;
  }
  if (up.contains('INVALID_ARGUMENT') ||
      up.contains('MODEL_NOT_FOUND') ||
      up.contains('NOT_FOUND')) {
    return GeminiErrorClass.modelError;
  }
  return GeminiErrorClass.unknown;
}

/// Retryable = RESOURCE_EXHAUSTED/429/UNAVAILABLE/503 and NOT daily-quota
/// (spec §3.3, telegram_bot.py:872-883), plus a client deadline hit /
/// transport failure (spec §3.2: "treated as a retryable network error")
/// and other 5xx statuses.
bool isRetryableGeminiError(String errorText) {
  final up = errorText.toUpperCase();
  if (isDailyFreeTierQuotaError(up)) return false;
  return up.contains('RESOURCE_EXHAUSTED') ||
      up.contains('429') ||
      up.contains('UNAVAILABLE') ||
      up.contains('503') ||
      up.contains('DEADLINE') ||
      up.contains('TIMEOUT') ||
      up.contains('CONNECTION') ||
      RegExp(r'HTTP 5\d\d').hasMatch(up);
}

/// Parse the server's retry hint — `retryDelay: "Ns"`, `retry in Ns`,
/// `retry in Nms` — ceil to int seconds, clamp [1, 60]; default 5
/// (spec §3.3, telegram_bot.py:852-869).
int parseRetryDelaySeconds(String errorText, {int fallback = 5}) {
  double? seconds;
  final ms = RegExp(r'retry in\s+(\d+(?:\.\d+)?)\s*ms', caseSensitive: false)
      .firstMatch(errorText);
  if (ms != null) {
    seconds = double.parse(ms.group(1)!) / 1000.0;
  } else {
    final m = RegExp('retryDelay[\'"]?\\s*:\\s*[\'"]?(\\d+(?:\\.\\d+)?)s',
                caseSensitive: false)
            .firstMatch(errorText) ??
        RegExp(r'retry in\s+(\d+(?:\.\d+)?)\s*s', caseSensitive: false)
            .firstMatch(errorText);
    if (m != null) seconds = double.parse(m.group(1)!);
  }
  if (seconds == null) return fallback.clamp(1, 60);
  return seconds.ceil().clamp(1, 60);
}

class GeminiAnalyzer implements AnalyzerService {
  GeminiAnalyzer(
    this._settings, {
    http.Client? client,
    Future<void> Function(Duration)? sleep,
    Future<Uint8List?> Function(Uint8List)? normalizer,
  })  : _client = client ?? http.Client(),
        _sleep = sleep ?? ((d) => Future<void>.delayed(d)),
        _normalize = normalizer ?? _computeNormalize;

  final AppSettings _settings;
  final http.Client _client;
  final Future<void> Function(Duration) _sleep;
  final Future<Uint8List?> Function(Uint8List) _normalize;

  /// Spec §3.3: GEMINI_ANALYSIS_MAX_ATTEMPTS = 3 (auto-intake path).
  static const int maxAttempts = 3;

  /// Spec §3.2: GEMINI_HTTP_DEADLINE_SECONDS = 90, hard client-side.
  static const Duration httpDeadline = Duration(seconds: 90);

  /// Spec §3.3: GEMINI_DAILY_QUOTA_COOLDOWN_SECONDS default 12 h.
  static const Duration dailyQuotaCooldown = Duration(hours: 12);

  /// Spec §3.1 step 6: on normalization failure, send the original only if
  /// it is under 5 MB, else fail.
  static const int maxOriginalFallbackBytes = 5 * 1024 * 1024;

  // Spec §3.1: normalization runs off the UI thread.
  static Future<Uint8List?> _computeNormalize(Uint8List bytes) =>
      compute(normalizeForAnalysis, bytes);

  Uri _endpoint(String key) => Uri.parse(
      'https://generativelanguage.googleapis.com/v1beta/models/${_settings.model}'
      ':generateContent?key=${Uri.encodeQueryComponent(key)}');

  /// One POST to generateContent (spec §3.2 request shape). Returns the raw
  /// response body on HTTP 200 — which also clears the daily-quota pause
  /// latch ("any later Gemini success clears the pause", spec §3.3). Throws
  /// [GeminiException] carrying classifiable error text otherwise.
  Future<String> _post(List<Map<String, Object?>> parts,
      {String? apiKeyOverride, Map<String, Object?>? generationConfig}) async {
    final key = (apiKeyOverride ?? _settings.geminiApiKey ?? '').trim();
    if (key.isEmpty) {
      throw const GeminiException('API_KEY missing: no Gemini key configured');
    }
    final body = jsonEncode({
      'contents': [
        {'parts': parts}
      ],
      // Spec §3.2: JSON mode guarantees syntax, not schema.
      'generationConfig':
          generationConfig ?? const {'response_mime_type': 'application/json'},
    });
    http.Response resp;
    try {
      resp = await _client
          .post(_endpoint(key),
              headers: const {'Content-Type': 'application/json'}, body: body)
          .timeout(httpDeadline);
    } on TimeoutException {
      // Spec §3.2: a deadline hit is a retryable network error.
      throw const GeminiException('DEADLINE: client-side 90s deadline hit');
    } catch (e) {
      throw GeminiException('CONNECTION error: $e');
    }
    if (resp.statusCode != 200) {
      throw GeminiException('HTTP ${resp.statusCode}: ${resp.body}');
    }
    if (_settings.quotaPauseUntil != null) {
      await _settings.setQuotaPauseUntil(null); // spec §3.3 pause-clear rule
    }
    return resp.body;
  }

  /// Extract `candidates[0].content.parts[0].text` (spec §3.2).
  String _extractText(String responseBody) {
    try {
      final decoded = jsonDecode(responseBody);
      final text =
          decoded['candidates'][0]['content']['parts'][0]['text'] as String;
      return text;
    } catch (_) {
      throw const GeminiException(
          'unexpected response shape: no candidates text');
    }
  }

  String _pausedMessage() {
    final until = _settings.quotaPauseUntil;
    if (until == null) return 'Gemini daily quota exhausted — analysis paused.';
    final remaining = until.difference(DateTime.now());
    final h = remaining.inHours;
    final m = remaining.inMinutes.remainder(60);
    // Spec §3.3 pause UX, simplified single-user wording.
    return 'Gemini daily free-tier quota exhausted — analysis paused until '
        '${until.toIso8601String()} (~${h}h ${m}m left). '
        'Photos are kept as failed and can be retried later.';
  }

  String _userMessageFor(String errorText) {
    switch (classifyGeminiError(errorText)) {
      case GeminiErrorClass.dailyQuotaExhausted:
        return _pausedMessage();
      case GeminiErrorClass.quotaRateLimit:
        return 'Gemini rate limit hit — please try again shortly.';
      case GeminiErrorClass.auth:
        return 'Gemini rejected the API key. Check the key in Settings.';
      case GeminiErrorClass.networkService:
        return 'Error contacting Gemini (network or service issue). '
            'Please try again.';
      case GeminiErrorClass.modelError:
        return 'Gemini model error — check the model name in Settings.';
      case GeminiErrorClass.unknown:
        return 'Gemini request failed. Please try again.';
    }
  }

  @override
  Future<AnalysisOutcome> analyzePhoto(Uint8List originalBytes) async {
    final sw = Stopwatch()..start();
    // Spec §3.3: while paused, skip analysis entirely — no model call. The
    // pause expires, so this is retryable by definition.
    if (_settings.isQuotaPaused) {
      return AnalysisOutcome(
          error: _pausedMessage(), retryable: true, wall: sw.elapsed);
    }

    Uint8List? sendBytes = await _normalize(originalBytes);
    if (sendBytes == null) {
      // Spec §3.1 step 6: fall back to original bytes only under 5 MB.
      if (originalBytes.length < maxOriginalFallbackBytes) {
        sendBytes = originalBytes;
      } else {
        return AnalysisOutcome(
            error: 'Could not process this photo (decode failed and it is '
                'too large to send unprocessed).',
            wall: sw.elapsed);
      }
    }

    // Spec §1.3: dietary profile appends to the photo prompt only.
    final prompt =
        withDietaryProfile(foodDetectionPrompt, _settings.dietaryProfile);
    final parts = <Map<String, Object?>>[
      {'text': prompt},
      {
        'inline_data': {
          'mime_type': 'image/jpeg',
          'data': base64Encode(sendBytes),
        }
      },
    ];

    for (var attempt = 1;; attempt++) {
      String text;
      try {
        text = _extractText(await _post(parts));
      } on GeminiException catch (e) {
        if (isDailyFreeTierQuotaError(e.message)) {
          // Spec §3.3: latch pause-until = now + cooldown, persisted. The
          // photo can succeed once the pause lifts → retryable.
          await _settings
              .setQuotaPauseUntil(DateTime.now().add(dailyQuotaCooldown),
                  forProvider: AiProvider.gemini);
          return AnalysisOutcome(
              error: _pausedMessage(), retryable: true, wall: sw.elapsed);
        }
        if (attempt < maxAttempts && isRetryableGeminiError(e.message)) {
          await _sleep(Duration(seconds: parseRetryDelaySeconds(e.message)));
          continue;
        }
        // Exhausted in-place retries on a transient class (429/5xx/network),
        // or the key is simply not configured yet: the PHOTO is fine either
        // way — a later scan may succeed. Auth/model/parse failures are not.
        final transient = isRetryableGeminiError(e.message) ||
            e.message.startsWith('API_KEY missing');
        return AnalysisOutcome(
            error: _userMessageFor(e.message),
            retryable: transient,
            wall: sw.elapsed);
      }

      // Spec §3.3/§3.4: a JSON parse failure is parse_error — never retried.
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
      // coerceIsFood gate (task contract): the ONLY mutation applied here —
      // totals/items stay raw; safeNumber/safeFoodItems are the consumers'
      // job (spec §3.5 parity).
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
    // Single attempt: spec §3.3's retry loop is the photo path; the text
    // handler surfaces transport errors to the user directly (spec §4 step 4).
    String text;
    try {
      text = _extractText(
          await _post(<Map<String, Object?>>[
        {'text': prompt}
      ]));
    } on GeminiException catch (e) {
      if (isDailyFreeTierQuotaError(e.message)) {
        await _settings
            .setQuotaPauseUntil(DateTime.now().add(dailyQuotaCooldown),
                  forProvider: AiProvider.gemini);
      }
      return null;
    }
    dynamic parsed;
    try {
      parsed = parseAiJson(text);
    } on FormatException {
      return null;
    }
    if (parsed is Map) return Map<String, dynamic>.from(parsed);
    // Spec §4.1: real models emit bare JSON arrays despite the prompt. The
    // Map-typed seam carries them as {'actions': [...]}: the NL normalizer
    // treats a map with no recognized single intent and a non-empty actions
    // list exactly like a bare array.
    if (parsed is List) return {'actions': parsed};
    return null; // scalar JSON: unusable response
  }

  @override
  Future<String?> validateKey(String apiKey) async {
    // Spec §3: a 1-token REAL generation — models.list does NOT catch region
    // blocks. Only the HTTP status matters; a MAX_TOKENS-truncated body with
    // no text part is still a valid key.
    try {
      await _post(
        const [
          {'text': 'ping'}
        ],
        apiKeyOverride: apiKey,
        generationConfig: const {'maxOutputTokens': 1},
      );
      return null;
    } on GeminiException catch (e) {
      // A quota-class response PROVES the key authenticated (bad keys get
      // 400/401/403, never 429) — report the key as accepted so it persists.
      // Refusing to save a valid key while the daily quota is spent locked
      // users out of onboarding for hours (hit live on the iPhone,
      // 2026-07-24). ONLY a genuine HTTP response ('HTTP <status>: ...')
      // may qualify: transport-error texts embed arbitrary strings —
      // including the typed key inside the request URL — which could
      // substring-match the 429/QUOTA classifier and persist garbage.
      if (e.message.startsWith('HTTP ')) {
        switch (classifyGeminiError(e.message)) {
          case GeminiErrorClass.quotaRateLimit:
          case GeminiErrorClass.dailyQuotaExhausted:
            return null;
          default:
            break;
        }
      }
      return _userMessageFor(e.message);
    }
  }

  @override
  Future<KeyProbe> probeKey(String apiKey) async {
    try {
      await _post(
        const [
          {'text': 'ping'}
        ],
        apiKeyOverride: apiKey,
        generationConfig: const {'maxOutputTokens': 1},
      );
      return const KeyProbe(KeyProbeResult.ok);
    } on GeminiException catch (e) {
      switch (classifyGeminiError(e.message)) {
        // Gemini's free tier has no balance to run out of; the daily cap
        // is the equivalent "the key is fine, you cannot spend now".
        case GeminiErrorClass.dailyQuotaExhausted:
          return KeyProbe(KeyProbeResult.outOfCredit,
              message: _pausedMessage());
        case GeminiErrorClass.quotaRateLimit:
        case GeminiErrorClass.networkService:
          // A DEADLINE/CONNECTION failure is a network problem, not a
          // rejected key — do not send the user to regenerate a fine key.
          return KeyProbe(KeyProbeResult.rateLimited,
              message: _userMessageFor(e.message));
        case GeminiErrorClass.auth:
        case GeminiErrorClass.modelError:
        case GeminiErrorClass.unknown:
          return KeyProbe(KeyProbeResult.rejected,
              message: _userMessageFor(e.message));
      }
    }
  }
}
