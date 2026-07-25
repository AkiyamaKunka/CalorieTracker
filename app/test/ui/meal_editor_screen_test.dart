// Editor + day-detail flows: edit an existing meal, add one by hand, delete
// with confirmation. These pin the wiring the pure-logic tests can't see —
// which DAO call each button makes, and that the day list reflects it.
import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/screens/day_detail_screen.dart';
import 'package:calorie_tracker/ui/screens/meal_editor_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

Meal meal({
  int id = 1,
  String date = '2026-07-24',
  String time = '01:30 PM',
  Map<String, dynamic>? analysis,
}) =>
    Meal(
      id: id,
      date: date,
      time: time,
      timestamp: '2026-07-24T13:30:00.000',
      source: 'app_photo',
      imageHash: 'h$id',
      analysis: analysis ??
          {
            'is_food': true,
            'meal_description': 'Chicken salad',
            'total_calories': 450,
            'total_protein_g': 50,
            'total_carbs_g': 12,
            'total_fat_g': 22,
          },
    );

Widget host(Widget child) => MaterialApp(home: child);

/// The editor is a long form; the default 800x600 test surface pushes the
/// item controls below the fold, where taps silently miss.
void useTallSurface(WidgetTester tester) {
  tester.view.physicalSize = const Size(1400, 4000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
}

void main() {
  DateTime clock() => DateTime(2026, 7, 24, 19, 5);

  testWidgets('editing totals saves through updateMealFields, keeping the id',
      (tester) async {
    final dao = FakeDao()..seed(meal());
    await tester.pumpWidget(host(
        MealEditorScreen(dao: dao, meal: dao.meals.first, now: clock)));

    await tester.enterText(find.byKey(const Key('editorCalories')), '520');
    await tester.enterText(find.byKey(const Key('editorProtein')), '55');
    await tester.tap(find.byKey(const Key('saveMealButton')));
    await tester.pumpAndSettle();

    expect(dao.fieldUpdates, hasLength(1));
    final (id, analysis, date, time) = dao.fieldUpdates.single;
    expect(id, 1);
    expect(analysis!['total_calories'], 520);
    expect(analysis['total_protein_g'], 55);
    expect(analysis['total_carbs_g'], 12); // untouched field survives
    expect(analysis['meal_description'], 'Chicken salad');
    expect(date, '2026-07-24');
    expect(time, '01:30 PM');
    expect(dao.saved, isEmpty); // an edit must never insert a second row
  });

  testWidgets('invalid input blocks the save and lists every problem',
      (tester) async {
    final dao = FakeDao()..seed(meal());
    await tester.pumpWidget(host(
        MealEditorScreen(dao: dao, meal: dao.meals.first, now: clock)));

    await tester.enterText(find.byKey(const Key('editorCalories')), 'abc');
    await tester.enterText(find.byKey(const Key('editorFat')), '-3');
    await tester.tap(find.byKey(const Key('saveMealButton')));
    await tester.pump();

    expect(find.byKey(const Key('editorErrors')), findsOneWidget);
    expect(find.textContaining('Calories must be a number'), findsOneWidget);
    expect(find.textContaining('Fat cannot be negative'), findsOneWidget);
    expect(dao.fieldUpdates, isEmpty);
  });

  testWidgets('adding a meal by hand inserts with source app_manual',
      (tester) async {
    final dao = FakeDao();
    await tester.pumpWidget(host(MealEditorScreen(
        dao: dao, initialDate: '2026-07-20', now: clock)));

    await tester.enterText(
        find.byKey(const Key('editorDescription')), 'Bowl of congee');
    await tester.enterText(find.byKey(const Key('editorCalories')), '320');
    await tester.enterText(find.byKey(const Key('editorCarbs')), '60');
    await tester.tap(find.byKey(const Key('saveMealButton')));
    await tester.pumpAndSettle();

    expect(dao.saved, hasLength(1));
    final saved = dao.saved.single;
    expect(saved.source, 'app_manual');
    expect(saved.date, '2026-07-20'); // the day the user came from
    expect(saved.imageHash, ''); // no photo → no ledger identity
    expect(saved.analysis['is_food'], isTrue);
    expect(saved.analysis['total_calories'], 320);
    expect(saved.analysis['meal_description'], 'Bowl of congee');
  });

  testWidgets('delete asks first, and cancelling changes nothing',
      (tester) async {
    final dao = FakeDao()..seed(meal());
    await tester.pumpWidget(host(
        MealEditorScreen(dao: dao, meal: dao.meals.first, now: clock)));

    await tester.tap(find.byKey(const Key('deleteMealButton')));
    await tester.pumpAndSettle();
    expect(find.text('Delete this meal?'), findsOneWidget);

    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(dao.meals, hasLength(1));

    await tester.tap(find.byKey(const Key('deleteMealButton')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('confirmDeleteMeal')));
    await tester.pumpAndSettle();
    expect(dao.meals, isEmpty);
  });

  testWidgets('item rows can be added, filled, summed into totals, removed',
      (tester) async {
    useTallSurface(tester);
    final dao = FakeDao();
    await tester.pumpWidget(host(MealEditorScreen(dao: dao, now: clock)));

    await tester.tap(find.byKey(const Key('addItemRow')));
    await tester.pump();
    await tester.enterText(find.byKey(const Key('itemName0')), 'Rice');
    await tester.enterText(find.byKey(const Key('itemCal0')), '200');
    await tester.enterText(find.byKey(const Key('itemCarb0')), '44');

    await tester.tap(find.byKey(const Key('addItemRow')));
    await tester.pump();
    await tester.enterText(find.byKey(const Key('itemName1')), 'Egg');
    await tester.enterText(find.byKey(const Key('itemCal1')), '80');
    await tester.enterText(find.byKey(const Key('itemPro1')), '7');

    await tester.tap(find.byKey(const Key('useItemTotals')));
    await tester.pump();
    expect(
        tester
            .widget<TextField>(find.byKey(const Key('editorCalories')))
            .controller!
            .text,
        '280');
    expect(
        tester
            .widget<TextField>(find.byKey(const Key('editorProtein')))
            .controller!
            .text,
        '7');

    // Removing the first row must not corrupt the second one's values.
    await tester.tap(find.byKey(const Key('removeItem0')));
    await tester.pump();
    expect(
        tester.widget<TextField>(find.byKey(const Key('itemName0'))).controller!.text,
        'Egg');

    await tester.tap(find.byKey(const Key('saveMealButton')));
    await tester.pumpAndSettle();
    final items = dao.saved.single.analysis['food_items'] as List;
    expect(items, hasLength(1));
    expect((items.single as Map)['name'], 'Egg');
  });

  testWidgets('day detail lists the day, opens the editor, and reloads',
      (tester) async {
    final dao = FakeDao()
      ..seed(meal(id: 1))
      ..seed(meal(
          id: 2,
          time: '07:15 PM',
          analysis: {
            'is_food': true,
            'meal_description': 'Noodles',
            'total_calories': 700,
          }));
    await tester.pumpWidget(host(
        DayDetailScreen(dao: dao, date: '2026-07-24', now: clock)));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('dayTotalKcal')), findsOneWidget);
    expect(find.text('~1,150 kcal'), findsOneWidget); // 450 + 700
    expect(find.text('2 meals'), findsOneWidget);
    expect(find.text('Noodles'), findsOneWidget);

    await tester.tap(find.byKey(const Key('mealRow2')));
    await tester.pumpAndSettle();
    expect(find.text('Edit meal'), findsOneWidget);

    await tester.enterText(find.byKey(const Key('editorCalories')), '650');
    await tester.tap(find.byKey(const Key('saveMealButton')));
    await tester.pumpAndSettle();

    // Back on the day, totals reflect the edit (450 + 650).
    expect(find.text('~1,100 kcal'), findsOneWidget);
  });

  testWidgets('empty day offers the add path', (tester) async {
    final dao = FakeDao();
    await tester.pumpWidget(host(
        DayDetailScreen(dao: dao, date: '2026-07-19', now: clock)));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('dayEmpty')), findsOneWidget);

    await tester.tap(find.byKey(const Key('addMealToDay')));
    await tester.pumpAndSettle();
    expect(find.text('Add meal'), findsOneWidget);
    // The new meal defaults to the day being viewed, not today.
    expect(find.widgetWithText(OutlinedButton, '2026-07-19'), findsOneWidget);
  });
}
