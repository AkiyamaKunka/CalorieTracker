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

/// The 2026-08-02 Apple restructure moved provider/key/model/server
/// controls onto their own page (progressive disclosure): tests that
/// exercise them walk through the root's AI Provider row first.
Future<void> openProviderPage(WidgetTester tester) async {
  await tester.tap(find.byKey(const Key('aiProviderRow')));
  await tester.pumpAndSettle();
}

void main() {
  connectClaudeTests();
  importExportTests();

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
    await openProviderPage(tester);
    expect(find.text('Test This Provider'), findsOneWidget);
    expect(find.byKey(const Key('diagnosticsTile')), findsNothing,
        reason: 'the tile was a third door to the same page');
    expect(find.text('Validate key'), findsNothing);
    expect(find.text('Key OK'), findsNothing);

    await tester.tap(find.byKey(const Key('testProviderButton')));
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
    await openProviderPage(tester);
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
    await openProviderPage(tester);
    // The segmented control became the Subscription checklist (user-
    // designed two-type IA, 2026-08-02): picking a plan row selects both
    // the server provider AND the backend.
    expect(find.byKey(const Key('planChoice-claude')), findsOneWidget);
    expect(settings.serverBackend, 'claude');

    await tester.tap(find.byKey(const Key('planChoice-glm')));
    await tester.pumpAndSettle();
    expect(settings.serverBackend, 'glm',
        reason: 'the tap must reach SettingsStore.update(serverBackend:)');
    expect(settings.provider, 'server');

    await tester.tap(find.byKey(const Key('planChoice-doubao')));
    await tester.pumpAndSettle();
    expect(settings.serverBackend, 'doubao');
  });

  testWidgets('picking a plan from an API provider switches to the server '
      'in one tap', (tester) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(apiKey: 'k'); // gemini default
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    await openProviderPage(tester);
    expect(find.byKey(const Key('serverBackendSelector')), findsNothing,
        reason: 'the segmented control is gone — plans are first-class rows');
    await tester.ensureVisible(find.byKey(const Key('planChoice-doubao')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('planChoice-doubao')));
    await tester.pumpAndSettle();
    expect(settings.provider, 'server');
    expect(settings.serverBackend, 'doubao');
  });

  testWidgets('model is PICKED from a curated list, not typed', (tester) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(apiKey: 'k'); // gemini, curated default
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    await openProviderPage(tester);
    expect(find.byKey(const Key('modelPicker')), findsOneWidget);
    expect(find.byKey(const Key('modelField')), findsNothing,
        reason: 'no raw text entry unless the user asks for Custom');

    await tester.ensureVisible(find.byKey(const Key('modelPicker')));
    await tester.pumpAndSettle();
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
    await openProviderPage(tester);
    expect(find.byKey(const Key('modelField')), findsOneWidget,
        reason: 'an unlisted stored model must stay visible and editable');
    expect(find.text('Custom — type a model name…'), findsOneWidget);
  });

  testWidgets('switching provider reloads BOTH the key and the model, and '
      'remounts the picker', (tester) async {
    // The dropdown's onChanged is the only place these three move
    // together; nothing tested it, so a dropped line would have shown
    // Gemini's key under Doubao (review 2026-07-31).
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(
      keys: {'gemini': 'gem-key', 'glm': 'glm-key'},
      models: {'gemini': 'gemini-2.5-flash', 'glm': 'glm-4.6v-flash'},
    );
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    await openProviderPage(tester);
    expect(
        tester
            .widget<TextField>(find.byKey(const Key('apiKeyField')))
            .controller!
            .text,
        'gem-key');

    // The dropdown became an Apple checkmark list (2026-08-02).
    await tester.tap(find.byKey(const Key('providerChoice-glm')));
    await tester.pumpAndSettle();

    expect(settings.provider, 'glm');
    expect(
        tester
            .widget<TextField>(find.byKey(const Key('apiKeyField')))
            .controller!
            .text,
        'glm-key',
        reason: "the key field must show the NEW provider's key");
    // The picker remounted onto the GLM list (its free flash default).
    expect(find.textContaining('glm-4.6v-flash'), findsWidgets);
    expect(find.byKey(const Key('modelField')), findsNothing,
        reason: 'the GLM default IS curated — no custom field');
  });

  testWidgets('enabling the watcher without a usable key warns instead of '
      'arming a switch that does nothing', (tester) async {
    tester.view.physicalSize = const Size(800, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(apiKey: '', watcherEnabled: false);
    final intake = FakeIntake();
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: settings,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      photoIntake: intake,
      requestPhotoPermission: () async => true,
    )));
    await tester.tap(find.byKey(const Key('watcherToggle')));
    await tester.pumpAndSettle();

    expect(settings.watcherEnabled, isTrue, reason: 'the toggle still arms');
    expect(intake.started, isTrue);
    expect(find.textContaining("won't be analyzed"), findsOneWidget);
    expect(intake.backfillScans, 0,
        reason: 'no key: reading photo bytes would be for nothing');
  });

  testWidgets('a quota pause gets its OWN warning wording', (tester) async {
    tester.view.physicalSize = const Size(800, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final paused = FakeSettings(apiKey: 'k')..isQuotaPaused = true;
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: paused,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      photoIntake: FakeIntake(),
      requestPhotoPermission: () async => true,
    )));
    await tester.tap(find.byKey(const Key('watcherToggle')));
    await tester.pumpAndSettle();
    // The BANNER also says "daily quota" — assert the snackbar's own words.
    expect(find.textContaining('new photos will wait'), findsOneWidget);
  });

  testWidgets('a healthy enable is silent and sweeps immediately',
      (tester) async {
    tester.view.physicalSize = const Size(800, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final healthy = FakeSettings(apiKey: 'k');
    final intake = FakeIntake();
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: healthy,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      photoIntake: intake,
      requestPhotoPermission: () async => true,
    )));
    await tester.tap(find.byKey(const Key('watcherToggle')));
    await tester.pumpAndSettle();
    expect(find.textContaining("won't be analyzed"), findsNothing);
    expect(intake.backfillScans, 1,
        reason: 'a healthy enable sweeps the window immediately');
  });

  testWidgets('first-run card shows only without a key, and the quota '
      'banner only while paused', (tester) async {
    final fresh = FakeSettings(apiKey: '');
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: fresh,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    expect(find.byKey(const Key('firstRunCard')), findsOneWidget);
    expect(find.textContaining('中国大陆用户'), findsOneWidget,
        reason: 'the mainland steer is the point of the card');
    expect(find.byKey(const Key('quotaPauseBanner')), findsNothing);
    // The card must not still name a button that no longer exists.
    expect(find.textContaining('Validate key'), findsNothing);

    final paused = FakeSettings(apiKey: 'k')
      ..isQuotaPaused = true
      ..quotaPauseUntil = DateTime(2026, 7, 31, 23, 30);
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: paused,
      analyzer: FakeAnalyzer(),
      dao: FakeDao(),
      requestPhotoPermission: () async => true,
    )));
    expect(find.byKey(const Key('firstRunCard')), findsNothing);
    // The banner moved WITH the provider controls to the AI Provider
    // page; the root footer still states the pause in prose.
    await openProviderPage(tester);
    expect(find.byKey(const Key('quotaPauseBanner')), findsOneWidget);
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

    await openProviderPage(tester);
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
    await openProviderPage(tester);
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
    await openProviderPage(tester);
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

void importExportTests() {
  testWidgets('import: a pasted export merges and reports what happened',
      (tester) async {
    tester.view.physicalSize = const Size(800, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final dao = FakeDao();
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: FakeSettings(apiKey: 'k'),
      analyzer: FakeAnalyzer(),
      dao: dao,
      requestPhotoPermission: () async => true,
    )));
    await tester.tap(find.byKey(const Key('importButton')));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byKey(const Key('importField')),
        '{"format":"calorie_tracker_export","version":1,'
        '"exported_at":"2026-07-30T10:00:00.000",'
        '"tables":{"meals":[{"date":"2026-07-30"}]}}');
    await tester.tap(find.byKey(const Key('importConfirm')));
    await tester.pumpAndSettle();

    expect(dao.imported, hasLength(1));
    expect(find.textContaining('Imported 1 meal'), findsOneWidget);
  });

  testWidgets('import: a wrong file is REFUSED with a readable reason',
      (tester) async {
    tester.view.physicalSize = const Size(800, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final dao = FakeDao();
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: FakeSettings(apiKey: 'k'),
      analyzer: FakeAnalyzer(),
      dao: dao,
      requestPhotoPermission: () async => true,
    )));
    await tester.tap(find.byKey(const Key('importButton')));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byKey(const Key('importField')), 'my shopping list');
    await tester.tap(find.byKey(const Key('importConfirm')));
    await tester.pumpAndSettle();

    expect(find.textContaining('not JSON'), findsOneWidget);
  });

  testWidgets('import: cancelling after PASTING writes nothing', (tester) async {
    // Cancelling an EMPTY dialog proves nothing (an unconditional import
    // of '' would also leave dao.imported empty by throwing) — paste a
    // valid payload first, so only the Cancel path can explain the
    // silence (review 2026-07-31).
    tester.view.physicalSize = const Size(800, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final dao = FakeDao();
    await tester.pumpWidget(_wrap(SettingsScreen(
      settings: FakeSettings(apiKey: 'k'),
      analyzer: FakeAnalyzer(),
      dao: dao,
      requestPhotoPermission: () async => true,
    )));
    await tester.tap(find.byKey(const Key('importButton')));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byKey(const Key('importField')),
        '{"format":"calorie_tracker_export","version":1,'
        '"tables":{"meals":[{"date":"2026-07-30"}]}}');
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(dao.imported, isEmpty,
        reason: 'a pasted-but-cancelled payload must not be written');
    expect(find.textContaining('Imported'), findsNothing);
  });
}
