/// REAL end-to-end integration test: drives the actual app (real main(),
/// real secure storage, real sqlite, real photo library, LIVE Gemini).
///
/// Preconditions (scripts/run_e2e_ios.sh handles all of them):
///  - app uninstalled from the simulator (fresh state → onboarding path),
///  - photo permission pre-granted (no system dialog),
///  - two food photos injected into the simulator gallery via addmedia,
///  - E2E_GEMINI_KEY provided via --dart-define-from-file (NEVER printed).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

import 'package:calorie_tracker/main.dart' as app;
import 'package:calorie_tracker/ui/widgets/meal_card.dart';

const String _apiKey = String.fromEnvironment('E2E_GEMINI_KEY');

/// Poll a finder with real frames; pumpAndSettle never settles while
/// spinners/streams are live, so explicit polling is the only safe wait.
Future<void> pumpUntilFound(
  WidgetTester tester,
  Finder finder, {
  required String what,
  Duration timeout = const Duration(seconds: 30),
  String Function()? probe,
}) async {
  final sw = Stopwatch()..start();
  while (sw.elapsed < timeout) {
    await tester.pump(const Duration(milliseconds: 250));
    if (finder.evaluate().isNotEmpty) return;
  }
  // Terse failure on purpose: no widget-tree dump (the tree can contain the
  // API key text field). Never include widget contents here beyond the
  // whitelisted probe markers.
  final state = probe == null ? '' : ' | visible state: ${probe()}';
  throw TestFailure(
      'Timed out after ${timeout.inSeconds}s waiting for: $what$state');
}

/// Whitelisted state markers for the Add-photo screen — safe to print
/// (never contains the API key).
String _addPhotoScreenProbe(WidgetTester tester) {
  final markers = <String>[];
  void mark(Finder f, String label) {
    if (f.evaluate().isNotEmpty) markers.add(label);
  }

  mark(find.text('From recent photos'), 'bottom sheet still open');
  mark(find.text('Recent photos'), 'Recent photos screen shown');
  mark(find.text('No recent photos found.'), 'empty-grid message');
  mark(find.byType(CircularProgressIndicator), 'spinner');
  final err = find.textContaining('Could not load photos');
  if (err.evaluate().isNotEmpty) {
    markers.add('error="${tester.widget<Text>(err.first).data}"');
  }
  final outcomeTitles = [
    'Meal logged',
    'No food detected',
    'Duplicate photo',
    'Already logged',
    'Analysis failed',
  ];
  for (final t in outcomeTitles) {
    mark(find.text(t), 'outcome dialog "$t"');
  }
  return markers.isEmpty ? 'none of the known markers' : markers.join(', ');
}

Future<void> pumpUntil(
  WidgetTester tester,
  bool Function() condition, {
  required String what,
  Duration timeout = const Duration(seconds: 30),
}) async {
  final sw = Stopwatch()..start();
  while (sw.elapsed < timeout) {
    await tester.pump(const Duration(milliseconds: 250));
    if (condition()) return;
  }
  throw TestFailure(
      'Timed out after ${timeout.inSeconds}s waiting for: $what');
}

void _pass(String stage, Stopwatch sw) {
  // ignore: avoid_print
  print('E2E PASS [$stage] in ${(sw.elapsedMilliseconds / 1000).toStringAsFixed(1)}s');
  sw
    ..reset()
    ..start();
}

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets(
    'full flow: onboarding -> key validation -> photo analysis -> NL correction -> history',
    (tester) async {
      expect(_apiKey.trim(), isNotEmpty,
          reason: 'E2E_GEMINI_KEY dart-define missing — run via '
              'scripts/run_e2e_ios.sh <env-file>');

      final sw = Stopwatch()..start();

      // ── Stage A: launch, fresh install lands on Settings (onboarding) ──
      await app.main();
      await pumpUntilFound(tester, find.byKey(const Key('apiKeyField')),
          what: 'Settings screen with API key field (onboarding landing)',
          timeout: const Duration(seconds: 60));
      _pass('A launch->onboarding Settings', sw);

      // ── Stage B: enter key, validate (REAL network call) ──
      await tester.tap(find.byKey(const Key('apiKeyField')));
      await tester.pump(const Duration(milliseconds: 300));
      await tester.enterText(find.byKey(const Key('apiKeyField')), _apiKey.trim());
      await tester.pump(const Duration(milliseconds: 300));
      await tester.tap(find.byKey(const Key('validateKeyButton')));
      await pumpUntilFound(tester, find.byKey(const Key('keyOkText')),
          what: 'key validation success marker (keyOkText)',
          timeout: const Duration(seconds: 90));
      _pass('B live key validation', sw);

      // ── Stage C: FAB add flow -> recent photos -> LIVE vision analysis ──
      await tester.tap(find.descendant(
          of: find.byType(NavigationBar), matching: find.text('Today')));
      await pumpUntilFound(tester, find.byKey(const Key('addMealFab')),
          what: 'Today tab with add FAB');
      await tester.tap(find.byKey(const Key('addMealFab')));
      await pumpUntilFound(tester, find.byKey(const Key('addFromPhotos')),
          what: 'add-flow bottom sheet');
      // Let the sheet's slide-in animation finish so the tap cannot miss.
      await tester.pump(const Duration(milliseconds: 500));
      await tester.tap(find.byKey(const Key('addFromPhotos')));
      await pumpUntilFound(tester, find.text('Recent photos'),
          what: 'Recent photos screen (route pushed)',
          timeout: const Duration(seconds: 15),
          probe: () => _addPhotoScreenProbe(tester));
      // Grid loads ORIGINAL bytes for up to 30 assets — give it time.
      await pumpUntilFound(tester, find.byKey(const Key('recentPhoto0')),
          what: 'first tile of the recent-photos grid',
          timeout: const Duration(seconds: 90),
          probe: () => _addPhotoScreenProbe(tester));
      await tester.pump(const Duration(milliseconds: 300));
      await tester.tap(find.byKey(const Key('recentPhoto0')));
      // LIVE Gemini vision call: analyzing overlay, then the outcome dialog.
      final analysisSw = Stopwatch()..start();
      await pumpUntilFound(tester, find.text('Meal logged'),
          what: '"Meal logged" outcome dialog after LIVE photo analysis',
          timeout: const Duration(seconds: 120),
          probe: () => _addPhotoScreenProbe(tester));
      // ignore: avoid_print
      print('E2E TIMING photo analysis wall: '
          '${(analysisSw.elapsedMilliseconds / 1000).toStringAsFixed(1)}s');
      // Dialog content: 'Logged meal #N: <description> — ~<kcal> kcal'.
      final dialogText = tester
          .widgetList<Text>(find.descendant(
              of: find.byType(AlertDialog), matching: find.byType(Text)))
          .map((t) => t.data ?? '')
          .join(' ');
      expect(RegExp(r'~\d+ kcal').hasMatch(dialogText), isTrue,
          reason: 'outcome dialog should contain a kcal estimate');
      expect(RegExp(r'Logged meal #\d+: .+ —').hasMatch(dialogText), isTrue,
          reason: 'outcome dialog should contain a meal description');
      await tester.pump(const Duration(milliseconds: 300)); // dialog settle
      await tester.tap(find.text('OK'));
      _pass('C photo -> live Gemini analysis -> meal saved', sw);

      // ── Stage D: Today totals header + meal card ──
      await pumpUntilFound(tester, find.byType(MealCard),
          what: 'meal card on the Today screen',
          timeout: const Duration(seconds: 60));
      final totalText = tester
              .widget<Text>(find.byKey(const Key('todayTotalKcal')))
              .data ??
          '';
      final kcal = int.tryParse(
              RegExp(r'^([\d,]+) kcal$').firstMatch(totalText)?.group(1)?.replaceAll(',', '') ??
                  '');
      expect(kcal, isNotNull, reason: 'totals header should read "<n> kcal"');
      expect(kcal! > 0, isTrue,
          reason: 'totals header should be nonzero after the photo meal');
      expect(find.byType(MealCard), findsOneWidget);
      _pass('D Today totals nonzero + one meal card', sw);

      // ── Stage E: NL correction box -> LIVE text intent -> 500 kcal ──
      await tester.tap(find.byKey(const Key('correctionField')));
      await tester.pump(const Duration(milliseconds: 300));
      await tester.enterText(find.byKey(const Key('correctionField')),
          'change that meal to exactly 500 kcal');
      await tester.pump(const Duration(milliseconds: 300));
      // The on-screen send button shifts while the software keyboard
      // animates, so a coordinate tap can miss. The keyboard "send" action
      // triggers the same _sendCorrection through onSubmitted, no geometry.
      await tester.testTextInput.receiveAction(TextInputAction.send);
      final nlSw = Stopwatch()..start();
      // Long NL replies present as a BLOCKING dialog (nl_presenter: >120
      // chars -> AlertDialog); reload() only runs after it is dismissed. So
      // poll AND dismiss reply dialogs as they appear.
      var sawReply = false;
      var updated = false;
      final replyTexts = <String>[];
      while (nlSw.elapsed < const Duration(seconds: 120)) {
        await tester.pump(const Duration(milliseconds: 250));
        final dialogOk = find.descendant(
            of: find.byType(AlertDialog), matching: find.text('OK'));
        if (dialogOk.evaluate().isNotEmpty) {
          sawReply = true;
          replyTexts.addAll(tester
              .widgetList<Text>(find.descendant(
                  of: find.byType(AlertDialog), matching: find.byType(Text)))
              .map((t) => t.data ?? ''));
          await tester.pump(const Duration(milliseconds: 300));
          await tester.tap(dialogOk, warnIfMissed: false);
          continue;
        }
        final snack = find.descendant(
            of: find.byType(SnackBar), matching: find.byType(Text));
        if (snack.evaluate().isNotEmpty) {
          sawReply = true;
          replyTexts.addAll(
              tester.widgetList<Text>(snack).map((t) => t.data ?? ''));
        }
        final f = find.byKey(const Key('todayTotalKcal'));
        if (f.evaluate().isNotEmpty &&
            (tester.widget<Text>(f).data ?? '') == '500 kcal') {
          updated = true;
          break;
        }
      }
      if (!updated) {
        // Reply texts come from the NL executor/model — never key material.
        throw TestFailure(
            'Timed out: Today totals never became "500 kcal" after the LIVE '
            'NL correction. sawReply=$sawReply '
            'replies=${replyTexts.toSet().join(' | ')}');
      }
      // ignore: avoid_print
      print('E2E TIMING NL correction wall: '
          '${(nlSw.elapsedMilliseconds / 1000).toStringAsFixed(1)}s');
      expect(find.byKey(const Key('correctedBadge')), findsOneWidget,
          reason: 'corrected meal should carry the corrected badge');
      _pass('E NL correction -> meal now 500 kcal', sw);

      // ── Stage F: History shows today's row ──
      // Drop the software keyboard first — it covers the NavigationBar.
      tester.binding.focusManager.primaryFocus?.unfocus();
      await tester.pump(const Duration(milliseconds: 800));
      await tester.tap(find.descendant(
          of: find.byType(NavigationBar), matching: find.text('History')));
      await pumpUntilFound(tester, find.byKey(const Key('historyAverage')),
          what: 'history average header',
          timeout: const Duration(seconds: 60),
          probe: () {
            final markers = <String>[];
            if (find
                .textContaining('No meals logged in the past')
                .evaluate()
                .isNotEmpty) {
              markers.add('empty-history message');
            }
            if (find.byKey(const Key('addMealFab')).evaluate().isNotEmpty) {
              markers.add('still on Today (FAB visible)');
            }
            return markers.isEmpty ? 'no known markers' : markers.join(', ');
          });
      expect(find.widgetWithText(ListTile, 'Today'), findsOneWidget,
          reason: "history should list today's row");
      expect(find.text('~500 kcal'), findsWidgets,
          reason: "today's history row should total ~500 kcal");
      _pass('F History shows today', sw);
    },
    timeout: const Timeout(Duration(minutes: 15)),
  );
}
