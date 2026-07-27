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

  testWidgets('the + button does not cover the chat box (user report '
      '2026-07-27)', (tester) async {
    // Today pins the correction chat box to the bottom of the body; a
    // standard Scaffold FAB floats bottom-right OVER the body — directly on
    // top of the chat box's send button.
    await tester.pumpWidget(
        CalorieTrackerApp(services: makeServices(settings: FakeSettings())));
    await tester.pumpAndSettle();
    final fab = tester.getRect(find.byKey(const Key('addMealFab')));
    final send = tester.getRect(find.byKey(const Key('correctionSend')));
    final field = tester.getRect(find.byKey(const Key('correctionField')));
    expect(fab.overlaps(send.inflate(4)), isFalse,
        reason: 'FAB $fab must not cover the send button $send');
    expect(fab.overlaps(field.inflate(4)), isFalse,
        reason: 'FAB $fab must not cover the text field $field');
  });

  testWidgets('shell boots to Settings when no API key is set (onboarding)',
      (tester) async {
    await tester.pumpWidget(CalorieTrackerApp(
        services: makeServices(settings: FakeSettings(apiKey: ''))));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('apiKeyField')), findsOneWidget);
  });
}
