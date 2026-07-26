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

  testWidgets('the primary button stays reachable with the keyboard up AND a '
      'long error (small phone)', (tester) async {
    // The old fixed Column pushed the button off-screen, where Flutter does
    // not even hit-test it: the user could neither retry nor read the error.
    tester.view.physicalSize = const Size(375, 667);
    tester.view.devicePixelRatio = 1.0;
    tester.view.viewInsets = const FakeViewPadding(bottom: 336); // keyboard
    addTearDown(tester.view.reset);

    executor.nextDescribe = const DescribeOutcome(
        error: '⏸️ Gemini is paused right now.\n\n'
            'Gemini daily free-tier quota is paused until 3:00 PM.\n\n'
            'Text corrections and manual meal parsing need Gemini too, so I '
            'did not send this request.');
    await tester.pumpWidget(host());
    await tester.enterText(find.byKey(const Key('addTextField')), 'eggs');
    await tester.tap(find.byKey(const Key('addTextSend')));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull, reason: 'no RenderFlex overflow');
    expect(find.byKey(const Key('addTextError')), findsOneWidget);
    // Scroll the page (not the field's own scroller) and tap the button —
    // proof it is reachable at all, which the fixed Column made impossible.
    await tester.drag(
        find.byType(SingleChildScrollView), const Offset(0, -300));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('addTextSend')));
    await tester.pumpAndSettle();
    expect(executor.describedTexts, hasLength(2));
  });

  testWidgets('a compound description warns that only the first meal is shown',
      (tester) async {
    executor.nextDescribe = const DescribeOutcome(
      analysis: {'is_food': true, 'total_calories': 300},
      warning: 'That described 2 meals — only the first is shown. '
          'Describe the others one at a time.',
    );
    await tester.pumpWidget(host());
    await tester.enterText(find.byKey(const Key('addTextField')),
        'eggs for breakfast, then a salad for lunch');
    await tester.tap(find.byKey(const Key('addTextSend')));
    await tester.pump(const Duration(milliseconds: 300));
    // Silence here would under-count the day with nothing to notice.
    expect(find.textContaining('only the first is shown'), findsOneWidget);
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
