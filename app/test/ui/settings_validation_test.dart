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
