/// Settings screen behavior: the ONE test action (diagnostics), key and
/// model persistence, the server backend selector, and the Claude OAuth
/// re-connect flow. The old 'Validate key' states are gone with the button
/// (2026-07-31) — the diagnostics page answers strictly more.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:calorie_tracker/ui/screens/settings_screen.dart';

import 'fakes.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  connectClaudeTests();

  testWidgets('ONE test action: the key field has no separate Validate — '
      'it opens the diagnostics page', (tester) async {
    // 'Validate key' ran the same analyzer.validateKey the diagnostics
    // page runs as stage 3 of six, and its persist-on-success job became
    // redundant when the field started persisting on type (2026-07-31).
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(apiKey: 'k');
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    expect(find.text('Test this provider'), findsOneWidget);
    expect(find.text('Validate key'), findsNothing);
    expect(find.text('Key OK'), findsNothing);

    await tester.tap(find.byKey(const Key('validateKeyButton')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('runDiagnostics')), findsOneWidget,
        reason: 'the one action opens the page that answers everything');
  });

  testWidgets('a typed key persists without any button', (tester) async {
    final settings = FakeSettings(apiKey: '');
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    await tester.enterText(find.byKey(const Key('apiKeyField')), 'my-key');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();
    expect(settings.apiKey, 'my-key');
  });
  testWidgets('server backend selector shows for the server provider and '
      'persists the choice', (tester) async {
    tester.view.physicalSize = const Size(800, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(apiKey: 'k')..provider = 'server';
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    expect(find.byKey(const Key('serverBackendSelector')), findsOneWidget);
    expect(settings.serverBackend, 'claude');

    await tester.tap(find.text('GLM'));
    await tester.pumpAndSettle();
    expect(settings.serverBackend, 'glm',
        reason: 'the tap must reach SettingsStore.update(serverBackend:)');

    await tester.tap(find.text('Doubao'));
    await tester.pumpAndSettle();
    expect(settings.serverBackend, 'doubao');
  });

  testWidgets('no backend selector away from the server provider',
      (tester) async {
    final settings = FakeSettings(apiKey: 'k'); // gemini default
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    expect(find.byKey(const Key('serverBackendSelector')), findsNothing);
  });

  testWidgets('model is PICKED from a curated list, not typed', (tester) async {
    final settings = FakeSettings(apiKey: 'k'); // gemini, curated default
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    expect(find.byKey(const Key('modelPicker')), findsOneWidget);
    expect(find.byKey(const Key('modelField')), findsNothing,
        reason: 'no raw text entry unless the user asks for Custom');

    await tester.tap(find.byKey(const Key('modelPicker')));
    await tester.pumpAndSettle();
    await tester
        .tap(find.text('gemini-2.5-pro — strongest, slower').last);
    await tester.pumpAndSettle();
    expect(settings.model, 'gemini-2.5-pro', reason: 'picking persists');

    // The Custom row reveals the text field for unlisted models.
    await tester.tap(find.byKey(const Key('modelPicker')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Custom — type a model name…').last);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('modelField')), findsOneWidget);
  });

  testWidgets('a stored unlisted model renders as Custom with the field '
      'visible', (tester) async {
    final settings =
        FakeSettings(apiKey: 'k', model: 'gemini-exp-something-new');
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    expect(find.byKey(const Key('modelField')), findsOneWidget,
        reason: 'an unlisted stored model must stay visible and editable');
    expect(find.text('Custom — type a model name…'), findsOneWidget);
  });

  testWidgets('watcher toggle stays off when permission is denied',
      (tester) async {
    final settings = FakeSettings(watcherEnabled: false);

    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => false,
    )));

    await tester.tap(find.byKey(const Key('watcherToggle')));
    await tester.pumpAndSettle();

    expect(settings.watcherEnabled, isFalse);
    expect(
        find.text(
            'Photo library permission is required for automatic intake.'),
        findsOneWidget);
  });
}

// ── Claude OAuth re-connect flow (server provider) ──────────────────

Widget _serverWrap({
  required FakeSettings settings,
  required Future<({String? url, String? error})> Function() start,
  required Future<String?> Function(String) complete,
  required Future<bool> Function(Uri) openUrl,
}) =>
    MaterialApp(
        home: Scaffold(
            body: SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
      startClaudeAuth: start,
      completeClaudeAuth: complete,
      openUrl: openUrl,
    )));

void connectClaudeTests() {
  testWidgets('connect: opens the OFFICIAL url, collects code, completes',
      (tester) async {
    tester.view.physicalSize = const Size(800, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(apiKey: 'k')..provider = 'server';
    final opened = <Uri>[];
    final completed = <String>[];
    await tester.pumpWidget(_serverWrap(
      settings: settings,
      start: () async =>
          (url: 'https://claude.ai/oauth/authorize?code=true', error: null),
      complete: (code) async {
        completed.add(code);
        return null;
      },
      openUrl: (uri) async {
        opened.add(uri);
        return true;
      },
    ));

    await tester.tap(find.byKey(const Key('connectClaudeButton')));
    // Fixed pumps: the busy spinner animates while the dialog is up, so
    // pumpAndSettle would wait forever.
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pump(const Duration(milliseconds: 300));
    // RFC 8252: the system browser got Anthropic's own page.
    expect(opened.single.host, 'claude.ai');
    expect(find.text('Finish connecting Claude'), findsOneWidget);

    await tester.enterText(
        find.byKey(const Key('claudeAuthCodeField')), ' abc#state ');
    await tester.tap(find.byKey(const Key('claudeAuthSubmit')));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pump(const Duration(milliseconds: 300));
    expect(completed.single, 'abc#state'); // trimmed
    expect(find.textContaining('Claude connected'), findsOneWidget);
  });

  testWidgets('connect: start failure surfaces the reason, no dialog',
      (tester) async {
    tester.view.physicalSize = const Size(800, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(apiKey: 'k')..provider = 'server';
    await tester.pumpWidget(_serverWrap(
      settings: settings,
      start: () async => (url: null, error: 'the CLI did not produce a URL'),
      complete: (_) async => fail('must not be called'),
      openUrl: (_) async => fail('must not be called'),
    ));
    await tester.tap(find.byKey(const Key('connectClaudeButton')));
    await tester.pumpAndSettle();
    expect(find.textContaining('did not produce'), findsOneWidget);
    expect(find.text('Finish connecting Claude'), findsNothing);
  });

  testWidgets('connect: cancelling the dialog completes nothing',
      (tester) async {
    tester.view.physicalSize = const Size(800, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(apiKey: 'k')..provider = 'server';
    var completions = 0;
    await tester.pumpWidget(_serverWrap(
      settings: settings,
      start: () async => (url: 'https://claude.ai/oauth/x', error: null),
      complete: (_) async {
        completions++;
        return null;
      },
      openUrl: (_) async => true,
    ));
    await tester.tap(find.byKey(const Key('connectClaudeButton')));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.tap(find.text('Cancel'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(completions, 0);
  });

  testWidgets('button hidden off the server provider and without wiring',
      (tester) async {
    final settings = FakeSettings(); // gemini
    await tester.pumpWidget(_serverWrap(
      settings: settings,
      start: () async => (url: null, error: null),
      complete: (_) async => null,
      openUrl: (_) async => true,
    ));
    expect(find.byKey(const Key('connectClaudeButton')), findsNothing);
  });
}
