/// Settings screen: live validateKey feedback states (loading → success /
/// error) against a fake analyzer.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:calorie_tracker/ui/screens/settings_screen.dart';

import 'fakes.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  connectClaudeTests();
  testWidgets('validate key shows loading then success', (tester) async {
    final analyzer = FakeAnalyzer();
    final completer = Completer<String?>();
    analyzer.onValidateKey = (_) => completer.future;
    final settings = FakeSettings(apiKey: '');

    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: analyzer,
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));

    await tester.enterText(find.byKey(const Key('apiKeyField')), 'my-key');
    await tester.tap(find.byKey(const Key('validateKeyButton')));
    await tester.pump();

    // Loading state while the future is pending.
    expect(find.byKey(const Key('keyValidating')), findsOneWidget);
    expect(find.byKey(const Key('keyValid')), findsNothing);

    completer.complete(null); // null = key OK (contract validateKey)
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('keyValid')), findsOneWidget);
    expect(find.byKey(const Key('keyOkText')), findsOneWidget);
    expect(analyzer.validatedKeys, ['my-key']);
    // A valid key is persisted.
    expect(settings.apiKey, 'my-key');
  });

  testWidgets('validate key shows the analyzer error', (tester) async {
    final analyzer = FakeAnalyzer();
    analyzer.onValidateKey = (_) async => 'API key not valid (403)';
    final settings = FakeSettings(apiKey: '');

    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: analyzer,
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));

    await tester.enterText(find.byKey(const Key('apiKeyField')), 'bad-key');
    await tester.tap(find.byKey(const Key('validateKeyButton')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('keyInvalid')), findsOneWidget);
    expect(find.text('API key not valid (403)'), findsOneWidget);
    // A rejected key is NOT persisted.
    expect(settings.apiKey, '');
  });

  testWidgets('empty key never calls the analyzer', (tester) async {
    final analyzer = FakeAnalyzer();

    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: FakeSettings(apiKey: ''),
      analyzer: analyzer,
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));

    await tester.tap(find.byKey(const Key('validateKeyButton')));
    await tester.pumpAndSettle();

    expect(analyzer.validatedKeys, isEmpty);
    expect(find.byKey(const Key('keyInvalid')), findsOneWidget);
  });

  testWidgets('server backend selector shows for the server provider and '
      'persists the choice', (tester) async {
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
    final settings = FakeSettings()..provider = 'server';
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
    final settings = FakeSettings()..provider = 'server';
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
    final settings = FakeSettings()..provider = 'server';
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
