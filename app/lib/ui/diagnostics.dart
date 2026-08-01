/// Provider diagnostics (user request 2026-07-31): "a page that user can
/// test the API, and know what's wrong" — quota, dead key, unreachable
/// endpoint, unsupported reply format, all named precisely.
///
/// Design: SIX staged checks, each mapping a failure class the provider
/// feedback matrix proved distinct, streamed as they complete so the UI
/// fills in live. Seam-clean (SettingsStore + AnalyzerService + injected
/// http.Client) so every stage is fake-testable.
library;

import 'dart:async';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:image/image.dart' as img;

import '../core/contracts.dart';
import 'services.dart';

enum DiagStatus { pass, warn, fail }

class DiagResult {
  const DiagResult(this.stage, this.status, this.summary,
      {this.detail, this.fix});
  final String stage;
  final DiagStatus status;
  final String summary; // one line, plain language
  final String? detail; // the raw evidence (verbatim error, host probed)
  final String? fix; // what the USER does next
}

/// Providers whose endpoints sit outside the mainland-China firewall.
const Set<String> _vpnNeeded = {'gemini', 'openai', 'anthropic'};

const Map<String, String> _probeUrls = {
  'gemini': 'https://generativelanguage.googleapis.com/',
  'openai': 'https://api.openai.com/',
  'anthropic': 'https://api.anthropic.com/',
  'qwen': 'https://dashscope.aliyuncs.com/',
  'doubao': 'https://ark.cn-beijing.volces.com/',
  'glm': 'https://open.bigmodel.cn/',
};

/// A real 64x64 JPEG (orange disc on white) generated with the same
/// library the analyzers use. The vision stage checks the CONTRACT — the
/// model answers the food-JSON schema — not the verdict; "not food" for an
/// orange disc is a PASS.
Uint8List defaultDiagnosticImage() {
  final image = img.Image(width: 64, height: 64);
  img.fill(image, color: img.ColorRgb8(255, 255, 255));
  img.fillCircle(image,
      x: 32, y: 32, radius: 20, color: img.ColorRgb8(230, 126, 34));
  return Uint8List.fromList(img.encodeJpg(image, quality: 85));
}

class ProviderDiagnostics {
  ProviderDiagnostics({
    required this.settings,
    required this.analyzer,
    http.Client? client,
    Uint8List Function()? testImage,
  })  : client = client ?? http.Client(),
        _testImage = testImage ?? defaultDiagnosticImage;

  final SettingsStore settings;
  final AnalyzerService analyzer;
  final http.Client client;
  final Uint8List Function() _testImage;

  /// Runs the staged checks, yielding each result as it lands. Stops early
  /// when a stage makes the rest meaningless (no key → nothing to test).
  Stream<DiagResult> run() async* {
    final provider = settings.provider;
    final isServer = provider == 'server';

    // 1 ── Configuration ────────────────────────────────────────────────
    if (isServer && settings.serverBaseUrl.isEmpty) {
      yield const DiagResult('Configuration', DiagStatus.fail,
          'No server address is set.',
          fix: 'Enter your server address in Settings, then re-run.');
      return;
    }
    if (settings.apiKey.trim().isEmpty) {
      yield DiagResult('Configuration', DiagStatus.fail,
          isServer
              ? 'No server upload key is set.'
              : 'No API key is set for this provider.',
          fix: 'Paste the key in Settings, then re-run.');
      return;
    }
    yield DiagResult(
        'Configuration',
        DiagStatus.pass,
        isServer
            ? 'Server address and upload key are set '
                '(backend: ${settings.serverBackend}).'
            : 'Provider "$provider" with a key and model '
                '"${settings.model}".');

    // 2 ── Endpoint reachability ────────────────────────────────────────
    final probeUrl =
        isServer ? settings.serverBaseUrl : _probeUrls[provider];
    if (probeUrl != null) {
      try {
        // ANY HTTP answer (404 included) proves the network path; only
        // transport failures mean unreachable.
        await client
            .get(Uri.parse(probeUrl))
            .timeout(const Duration(seconds: 10));
        yield DiagResult('Endpoint reachability', DiagStatus.pass,
            'The ${isServer ? 'server' : provider} endpoint answered.',
            detail: probeUrl);
      } catch (e) {
        yield DiagResult(
            'Endpoint reachability',
            DiagStatus.fail,
            'Could not reach ${isServer ? 'your server' : provider} '
                'at all.',
            detail: '$probeUrl — $e',
            fix: _vpnNeeded.contains(provider)
                ? 'This provider is blocked in mainland China without a '
                    'VPN. Turn the VPN on, or switch to Qwen/Doubao/GLM '
                    '(no VPN needed).'
                : isServer
                    ? 'Check the server address, that the server is '
                        'running, and your network.'
                    : 'Check your network connection and try again.');
        return;
      }
    }

    // 3 ── Authentication & account ─────────────────────────────────────
    // probeKey, NOT validateKey: validateKey deliberately ACCEPTS a
    // quota-class reply (it proves the key authenticated), so an empty
    // account looked identical to a healthy one and this stage could
    // never name the out-of-credit case the page promises to name.
    final probe = await analyzer.probeKey(settings.apiKey);
    switch (probe.result) {
      case KeyProbeResult.ok:
        yield const DiagResult('Authentication', DiagStatus.pass,
            'The provider accepted your key.');
      case KeyProbeResult.outOfCredit:
        yield DiagResult('Authentication', DiagStatus.warn,
            'The key works, but the account cannot pay right now.',
            detail: probe.message,
            fix: 'Top up the provider account, or switch to a free tier '
                "(Zhipu GLM's default vision model is free, no VPN "
                'needed in mainland China).');
      case KeyProbeResult.rateLimited:
        yield DiagResult('Authentication', DiagStatus.warn,
            'The key works, but the provider is rate-limiting right now.',
            detail: probe.message,
            fix: 'Wait a minute and re-run; photos are kept and retried '
                'automatically meanwhile.');
      case KeyProbeResult.rejected:
        yield DiagResult(
            'Authentication', DiagStatus.fail, 'The key was not accepted.',
            detail: probe.message,
            fix: 'Re-copy the key from the provider console — and check it '
                'belongs to THIS provider (keys are not interchangeable).');
        return;
    }

    // 4 ── Text round-trip ──────────────────────────────────────────────
    final text = await analyzer.textIntent(
        'Respond with ONLY this exact JSON object: {"ok": true}');
    yield text != null
        ? const DiagResult('Text analysis', DiagStatus.pass,
            'The model answered JSON — chat fixes and "describe a meal" '
            'work.')
        : const DiagResult('Text analysis', DiagStatus.warn,
            'The model did not return usable JSON for a text request.',
            detail: 'Chat fixes and "describe a meal" may fail; photo '
                'analysis can still work.',
            fix: 'If this persists, pick a different model in Settings.');

    // 5 ── Photo round-trip ─────────────────────────────────────────────
    final photo = await analyzer.analyzePhoto(_testImage());
    if (photo.analysis != null) {
      yield DiagResult('Photo analysis', DiagStatus.pass,
          'The model analyzed a test image and answered the meal format.',
          detail: photo.isFood
              ? 'It even thought the test disc was food.'
              : 'Verdict "not food" — correct for the test image.');
    } else {
      yield DiagResult(
          'Photo analysis',
          DiagStatus.fail,
          photo.retryable
              ? 'Photo analysis failed with a TEMPORARY problem.'
              : 'Photo analysis failed and a retry will NOT fix it.',
          detail: photo.error,
          fix: photo.retryable
              ? 'Usually a rate limit or a busy server — photos are kept '
                  'and retried automatically.'
              : 'Read the message above — it names the broken piece '
                  '(model, format, or account).');
    }

    // 6 ── Quota state ──────────────────────────────────────────────────
    if (settings.isQuotaPaused) {
      final until = settings.quotaPauseUntil;
      yield DiagResult('Quota', DiagStatus.warn,
          'Analyses are PAUSED — the daily quota was hit.',
          detail: until != null ? 'Paused until $until.' : null,
          fix: 'Wait it out (photos are kept), or change the key or '
              'provider to resume immediately.');
    } else {
      yield const DiagResult(
          'Quota', DiagStatus.pass, 'No quota pause is active.');
    }
  }
}
