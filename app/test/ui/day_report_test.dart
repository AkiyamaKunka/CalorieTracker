// The report-to-my-coach image (2026-08-02): a day's intake as one tall
// PNG. Pins the card's content contract and that rasterization actually
// produces a PNG — a silent blank image sent to a coach is worse than an
// error.
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/screens/today_screen.dart';
import 'package:calorie_tracker/ui/widgets/day_report.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

Meal _meal(int id, String time, String desc, num kcal,
        {String hash = ''}) =>
    Meal(
      id: id,
      date: '2026-08-02',
      time: time,
      timestamp: '2026-08-02T12:00:00',
      source: 'app_photo',
      imageHash: hash,
      analysis: {
        'is_food': true,
        'total_calories': kcal,
        'total_protein_g': 20,
        'total_carbs_g': 50,
        'total_fat_g': 10,
        'meal_description': desc,
        'food_items': [
          {'name': 'Rice (~150 g)', 'estimated_calories': 200},
        ],
      },
    );

void main() {
  testWidgets('the card carries the coach-relevant facts', (tester) async {
    tester.view.physicalSize = const Size(420, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(MaterialApp(
      home: SingleChildScrollView(
        child: DayReportCard(
          date: '2026-08-02',
          meals: [
            ReportMeal(meal: _meal(1, '8:05 AM', 'Soy milk and youtiao', 380)),
            ReportMeal(meal: _meal(2, '12:20 PM', 'Tomato egg noodles', 560)),
          ],
          typicalKcal: 2020,
          burnKcal: 88,
        ),
      ),
    ));
    expect(find.text('Daily intake'), findsOneWidget);
    expect(find.text('940 kcal'), findsOneWidget, reason: 'day total');
    expect(find.text('Soy milk and youtiao'), findsOneWidget);
    expect(find.text('Tomato egg noodles'), findsOneWidget);
    expect(find.text('8:05 AM'), findsOneWidget);
    expect(find.textContaining('Rice (~150 g)'), findsWidgets,
        reason: 'portion assumptions reach the coach');
    expect(find.text('+88'), findsOneWidget, reason: 'burn in the math');
    expect(find.text('Logged with CalorieTracker'), findsOneWidget);
  });

  testWidgets('an empty day still renders honestly', (tester) async {
    await tester.pumpWidget(const MaterialApp(
      home: SingleChildScrollView(
        child: DayReportCard(date: '2026-08-02', meals: []),
      ),
    ));
    expect(find.text('No meals logged.'), findsOneWidget);
  });

  testWidgets('renderDayReport produces a real PNG', (tester) async {
    await tester.runAsync(() async {
      final png = await renderDayReport(
        date: '2026-08-02',
        meals: [
          ReportMeal(meal: _meal(1, '8:05 AM', 'Soy milk and youtiao', 380)),
        ],
        typicalKcal: 2020,
      );
      expect(png.length, greaterThan(1000));
      // PNG magic bytes — the file a coach receives must BE an image.
      expect(png.sublist(0, 8),
          Uint8List.fromList([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]));
    });
  });

  testWidgets('the share button appears only when meals exist',
      (tester) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final dao = FakeDao();
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: TodayScreen(
                key: UniqueKey(), dao: dao, executor: FakeExecutor()))));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('shareDayButton')), findsNothing,
        reason: 'an empty day has nothing to report');

    dao.put(_meal(0, '8:05 AM', 'Soy milk', 110));
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: TodayScreen(
                key: UniqueKey(), dao: dao, executor: FakeExecutor()))));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('shareDayButton')), findsOneWidget);
  });
}
