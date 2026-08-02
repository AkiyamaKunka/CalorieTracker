/// App-shell smoke test with in-memory fakes (real wiring lives in
/// ui/di.dart and is exercised at integration time).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:calorie_tracker/ui/app.dart';

import 'ui/fakes.dart';

void main() {
  testWidgets('shell boots to Today with an API key configured',
      (tester) async {
    await tester.pumpWidget(
        CalorieTrackerApp(services: makeServices(settings: FakeSettings())));
    await tester.pumpAndSettle();
    expect(find.text('Today'), findsWidgets);
    expect(find.byKey(const Key('addMealFab')), findsOneWidget);
  });

  testWidgets('the Meals button owns all four meal actions '
      '(chat bar is gone)', (tester) async {
    await tester.pumpWidget(
        CalorieTrackerApp(services: makeServices(settings: FakeSettings())));
    await tester.pumpAndSettle();
    // The pinned correction bar was removed 2026-07-31 (user request);
    // its feature lives in the sheet.
    expect(find.byKey(const Key('correctionField')), findsNothing);
    await tester.tap(find.byKey(const Key('addMealFab')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('addFromPhotos')), findsOneWidget);
    expect(find.byKey(const Key('addFromText')), findsOneWidget);
    expect(find.byKey(const Key('addManually')), findsOneWidget);
    expect(find.byKey(const Key('addFixMeal')), findsOneWidget);
  });

  testWidgets('every Meals-sheet tile actually ROUTES somewhere',
      (tester) async {
    // The sheet's presentation was asserted; none of the four branches was
    // ever taken, so any tile could silently become a dead button
    // (mutating `choice == 'fix'` to a never-matching string stayed green).
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final services = makeServices(settings: FakeSettings());

    Future<void> openSheetAndTap(String tileKey) async {
      await tester.tap(find.byKey(const Key('addMealFab')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(Key(tileKey)));
      await tester.pumpAndSettle();
    }

    await tester.pumpWidget(CalorieTrackerApp(services: services));
    await tester.pumpAndSettle();

    await openSheetAndTap('addFromText');
    expect(find.byKey(const Key('addTextField')), findsOneWidget,
        reason: 'Describe a meal → the describe screen');
    await tester.pageBack();
    await tester.pumpAndSettle();

    await openSheetAndTap('addFixMeal');
    expect(find.byKey(const Key('fixMealField')), findsOneWidget,
        reason: 'Fix or delete → the corrections screen');
    await tester.pageBack();
    await tester.pumpAndSettle();

    await openSheetAndTap('addManually');
    expect(find.byKey(const Key('saveMealButton')), findsOneWidget,
        reason: 'Enter manually → the editor, no AI involved');
    await tester.pageBack();
    await tester.pumpAndSettle();

    await openSheetAndTap('addFromPhotos');
    expect(find.text('Recent photos'), findsOneWidget);
  });

  testWidgets('shell boots to Settings when no API key is set (onboarding)',
      (tester) async {
    await tester.pumpWidget(CalorieTrackerApp(
        services: makeServices(settings: FakeSettings(apiKey: ''))));
    await tester.pumpAndSettle();
    // The Apple restructure (2026-08-02) put the key field one disclosure
    // deep: onboarding lands on Settings, whose welcome card points at the
    // AI Provider row; the field lives on that page.
    expect(find.byKey(const Key('firstRunCard')), findsOneWidget);
    await tester.tap(find.byKey(const Key('aiProviderRow')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('apiKeyField')), findsOneWidget);
  });
}
