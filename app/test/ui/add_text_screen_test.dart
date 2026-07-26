// Describe-a-meal screen: text → estimate → PREVIEW in the editor → save.
// The preview step is the point: a text estimate is a guess about a meal the
// model never saw, so nothing may reach the log unreviewed.
import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/screens/add_flow.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

void main() {
  late FakeDao dao;
  late FakeExecutor executor;

  setUp(() {
    dao = FakeDao();
    executor = FakeExecutor();
  });

  Widget host() =>
      MaterialApp(home: AddTextScreen(executor: executor, dao: dao));

  testWidgets('estimate opens the editor prefilled; save inserts the meal',
      (tester) async {
    await tester.pumpWidget(host());
    await tester.enterText(
        find.byKey(const Key('addTextField')), '  beef noodle soup  ');
    await tester.tap(find.byKey(const Key('addTextSend')));
    await tester.pumpAndSettle();

    // Trimmed text reached the describe path…
    expect(executor.describedTexts.single, 'beef noodle soup');
    // …and the editor opened with the estimate, having saved NOTHING yet.
    expect(find.byKey(const Key('editorCalories')), findsOneWidget);
    expect(dao.saved, isEmpty, reason: 'preview must not persist');
    expect(find.text('650'), findsOneWidget); // prefilled total

    await tester.tap(find.byKey(const Key('saveMealButton')));
    await tester.pumpAndSettle();

    expect(dao.saved, hasLength(1));
    final meal = dao.saved.single;
    expect(meal.source, 'manual_text'); // server parity, spec §4.6
    expect(meal.imageHash, isEmpty); // text meals carry no photo identity
    expect(meal.analysis['total_calories'], 650);
    expect(meal.analysis['meal_description'], 'Beef noodle soup');
    expect(dao.fieldUpdates, isEmpty, reason: 'insert, never an in-place edit');
  });

  testWidgets('the user can correct the estimate before it is saved',
      (tester) async {
    await tester.pumpWidget(host());
    await tester.enterText(find.byKey(const Key('addTextField')), 'noodles');
    await tester.tap(find.byKey(const Key('addTextSend')));
    await tester.pumpAndSettle();

    await tester.enterText(find.byKey(const Key('editorCalories')), '520');
    await tester.tap(find.byKey(const Key('saveMealButton')));
    await tester.pumpAndSettle();

    expect(dao.saved.single.analysis['total_calories'], 520);
  });

  testWidgets('a failed estimate shows the reason and opens no editor',
      (tester) async {
    executor.nextDescribe =
        const DescribeOutcome(error: "🚫 I couldn't detect food in that.");
    await tester.pumpWidget(host());
    await tester.enterText(find.byKey(const Key('addTextField')), 'a rock');
    await tester.tap(find.byKey(const Key('addTextSend')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('addTextError')), findsOneWidget);
    expect(find.byKey(const Key('editorCalories')), findsNothing);
    expect(dao.saved, isEmpty);
  });

  testWidgets('empty input never calls the model', (tester) async {
    await tester.pumpWidget(host());
    await tester.tap(find.byKey(const Key('addTextSend')));
    await tester.pumpAndSettle();
    expect(executor.describedTexts, isEmpty);
  });

  testWidgets('cancelling the editor leaves the log untouched', (tester) async {
    await tester.pumpWidget(host());
    await tester.enterText(find.byKey(const Key('addTextField')), 'eggs');
    await tester.tap(find.byKey(const Key('addTextSend')));
    await tester.pumpAndSettle();

    // Back out of the editor without saving.
    await tester.pageBack();
    await tester.pumpAndSettle();
    expect(dao.saved, isEmpty);
    // Still on the describe screen, text intact for another try.
    expect(find.byKey(const Key('addTextField')), findsOneWidget);
  });
}
