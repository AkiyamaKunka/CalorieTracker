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

  testWidgets('shell boots to Settings when no API key is set (onboarding)',
      (tester) async {
    await tester.pumpWidget(CalorieTrackerApp(
        services: makeServices(settings: FakeSettings(apiKey: ''))));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('apiKeyField')), findsOneWidget);
  });
}
