// The staged provider checks behind the "Test AI provider" page. Each
// stage maps a distinct failure class from the provider feedback matrix;
// these tests prove each class lands in the right stage with an
// actionable fix — the page exists because a refused backend spent an
// hour looking like "the API is broken" (2026-07-31).
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/diagnostics.dart';
import 'package:calorie_tracker/ui/screens/diagnostics_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'fakes.dart';

Uint8List _img() => Uint8List.fromList(List.filled(32, 9));

ProviderDiagnostics _diag({
  FakeSettings? settings,
  FakeAnalyzer? analyzer,
  http.Client? client,
}) =>
    ProviderDiagnostics(
      settings: settings ?? FakeSettings(),
      analyzer: analyzer ?? _okAnalyzer(),
      client: client ?? MockClient((_) async => http.Response('ok', 404)),
      testImage: _img,
    );

FakeAnalyzer _okAnalyzer() => FakeAnalyzer()
  ..nextTextIntent = {'ok': true}
  ..nextPhotoOutcome = const AnalysisOutcome(
      analysis: {'is_food': false, 'reason': 'a test disc'},
      isFood: false,
      wall: Duration.zero);

void main() {
  test('all six stages pass on a healthy setup', () async {
    final results = await _diag().run().toList();
    expect(results, hasLength(6));
    expect(results.map((r) => r.status).toSet(), {DiagStatus.pass});
    expect(results.first.stage, 'Configuration');
    expect(results.last.stage, 'Quota');
  });

  test('missing key stops at Configuration with a fix', () async {
    final results =
        await _diag(settings: FakeSettings(apiKey: '')).run().toList();
    expect(results, hasLength(1));
    expect(results.single.status, DiagStatus.fail);
    expect(results.single.fix, contains('Settings'));
  });

  test('server provider without an address stops at Configuration',
      () async {
    final settings = FakeSettings(apiKey: 'k')..provider = 'server';
    final results = await _diag(settings: settings).run().toList();
    expect(results.single.summary, contains('server address'));
  });

  test('unreachable intl endpoint names the VPN, and stops', () async {
    final results = await _diag(
      client: MockClient((_) async => throw Exception('network is down')),
    ).run().toList();
    expect(results, hasLength(2));
    expect(results.last.stage, 'Endpoint reachability');
    expect(results.last.status, DiagStatus.fail);
    expect(results.last.fix, contains('VPN'),
        reason: 'gemini is unreachable from mainland China without one');
  });

  test('rejected key stops at Authentication; billing only WARNS and '
      'continues', () async {
    final rejected = _okAnalyzer()
      ..onValidateKey = (_) async => 'The provider rejected the API key.';
    var results = await _diag(analyzer: rejected).run().toList();
    expect(results.last.stage, 'Authentication');
    expect(results.last.status, DiagStatus.fail);

    final broke = _okAnalyzer()
      ..onValidateKey =
          (_) async => 'Provider balance or credits exhausted — check '
              'your billing.';
    results = await _diag(analyzer: broke).run().toList();
    expect(
        results
            .firstWhere((r) => r.stage == 'Authentication')
            .status,
        DiagStatus.warn);
    expect(results, hasLength(6),
        reason: 'out-of-credit is a warning, not a dead stop — the later '
            'stages still tell the user what else works');
  });

  test('junk text reply warns; photo failure carries the verbatim error '
      'and retryability', () async {
    final analyzer = _okAnalyzer()
      ..nextTextIntent = null
      ..nextPhotoOutcome = const AnalysisOutcome(
          error: 'Doubao model not found — Ark needs the EXACT versioned '
              'ID from its model list.',
          retryable: false,
          wall: Duration.zero);
    final results = await _diag(analyzer: analyzer).run().toList();
    expect(results.firstWhere((r) => r.stage == 'Text analysis').status,
        DiagStatus.warn);
    final photo = results.firstWhere((r) => r.stage == 'Photo analysis');
    expect(photo.status, DiagStatus.fail);
    expect(photo.summary, contains('retry will NOT fix'));
    expect(photo.detail, contains('EXACT versioned ID'),
        reason: 'the provider-specific message must reach the user');
  });

  test('quota pause surfaces as a warning with the until-time', () async {
    final settings = FakeSettings()
      ..isQuotaPaused = true
      ..quotaPauseUntil = DateTime(2026, 7, 31, 22, 0);
    final results = await _diag(settings: settings).run().toList();
    final quota = results.firstWhere((r) => r.stage == 'Quota');
    expect(quota.status, DiagStatus.warn);
    expect(quota.detail, contains('2026-07-31'));
  });

  testWidgets('the screen streams results and renders the verdict',
      (tester) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(MaterialApp(
        home: DiagnosticsScreen(diagnostics: _diag())));
    await tester.tap(find.byKey(const Key('runDiagnostics')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('diagVerdict')), findsOneWidget);
    expect(find.textContaining('Everything works'), findsOneWidget);
    expect(find.text('Photo analysis'), findsOneWidget);
  });

  testWidgets('problems show the count and the fixes', (tester) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final analyzer = _okAnalyzer()
      ..nextPhotoOutcome = const AnalysisOutcome(
          error: 'quota hit', retryable: true, wall: Duration.zero);
    await tester.pumpWidget(MaterialApp(
        home: DiagnosticsScreen(
            diagnostics: _diag(analyzer: analyzer))));
    await tester.tap(find.byKey(const Key('runDiagnostics')));
    await tester.pumpAndSettle();
    expect(find.textContaining('1 problem'), findsOneWidget);
    expect(find.textContaining('TEMPORARY'), findsOneWidget);
  });
}
