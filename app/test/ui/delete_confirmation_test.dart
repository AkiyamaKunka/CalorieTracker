/// The delete-confirmation modal flow (spec §4.5): the executor stages a
/// delete → the UI shows a modal listing the labels → Delete calls
/// confirmPendingDelete with the staged ids; Cancel calls nothing.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/screens/fix_meal_screen.dart';

import 'fakes.dart';

// The correction bar moved off Today into FixMealScreen (2026-07-31);
// the modal contract is unchanged.
Future<void> _pumpTodayAndSend(
    WidgetTester tester, FakeExecutor executor, String text) async {
  await tester.pumpWidget(MaterialApp(
    home: FixMealScreen(executor: executor),
  ));
  await tester.pumpAndSettle();
  await tester.enterText(find.byKey(const Key('fixMealField')), text);
  await tester.tap(find.byKey(const Key('fixMealSend')));
  await tester.pumpAndSettle();
}

void main() {
  final stagedReply = NlReply(
    'Deleting 2 meals as requested.',
    needsDeleteConfirmation: true,
    pendingDeleteIds: const [7, 9],
    pendingDeleteLabels: const [
      'Pizza (2026-07-17 12:10 PM, ~800 kcal)',
      'Cola (2026-07-17 12:12 PM, ~150 kcal)',
    ],
  );

  testWidgets('confirm calls confirmPendingDelete with the staged ids',
      (tester) async {
    final executor = FakeExecutor()
      ..nextReplies = [stagedReply]
      ..confirmResult = 'Deleted 2 meal(s).';

    await _pumpTodayAndSend(tester, executor, 'delete meal 0 and 1');

    expect(executor.handledTexts, ['delete meal 0 and 1']);
    // Modal lists every staged label + the warning (spec §4.5).
    expect(find.text('Delete meals?'), findsOneWidget);
    expect(find.textContaining('Pizza (2026-07-17 12:10 PM, ~800 kcal)'),
        findsOneWidget);
    expect(find.textContaining('Cola (2026-07-17 12:12 PM, ~150 kcal)'),
        findsOneWidget);
    expect(find.text('This cannot be undone.'), findsOneWidget);
    // Nothing deleted before confirmation.
    expect(executor.confirmedDeletes, isEmpty);

    await tester.tap(find.byKey(const Key('nlDeleteConfirm')));
    await tester.pumpAndSettle();

    expect(executor.confirmedDeletes, [
      [7, 9]
    ]);
    // The executor's result is surfaced.
    expect(find.text('Deleted 2 meal(s).'), findsOneWidget);
  });

  testWidgets('cancel never calls confirmPendingDelete', (tester) async {
    final executor = FakeExecutor()..nextReplies = [stagedReply];

    await _pumpTodayAndSend(tester, executor, 'delete everything');

    expect(find.text('Delete meals?'), findsOneWidget);
    await tester.tap(find.byKey(const Key('nlDeleteCancel')));
    await tester.pumpAndSettle();

    expect(executor.confirmedDeletes, isEmpty);
    expect(find.text('Delete meals?'), findsNothing);
  });

  testWidgets('plain replies render as a snackbar, no modal', (tester) async {
    final executor = FakeExecutor()
      ..nextReplies = [const NlReply('Logged 72.5 kg for 2026-07-17.')];

    await _pumpTodayAndSend(tester, executor, 'I weigh 72.5 kg');

    expect(find.text('Delete meals?'), findsNothing);
    expect(find.text('Logged 72.5 kg for 2026-07-17.'), findsOneWidget);
    expect(executor.confirmedDeletes, isEmpty);
  });
}
