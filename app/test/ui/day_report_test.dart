// The report-to-my-coach image (2026-08-02): a day's intake as one tall
// PNG. Pins the card's content contract and that rasterization actually
// produces a PNG — a silent blank image sent to a coach is worse than an
// error.
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/format.dart' show isoDate;
import 'package:calorie_tracker/ui/l10n.dart';
import 'package:calorie_tracker/ui/screens/today_screen.dart';
import 'package:calorie_tracker/ui/widgets/day_report.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

Meal _meal(int id, String time, String desc, num kcal,
        {String hash = '', String date = '2026-08-02'}) =>
    Meal(
      id: id,
      date: date,
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
    expect(find.text('Logged with Bitewise'), findsOneWidget);
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

  testWidgets('a zh user\'s coach gets a zh report (detached-tree locale)',
      (tester) async {
    // The card renders OUTSIDE the app's widget tree, so it can't inherit
    // the app locale — renderDayReport must build its own Localizations
    // scope. Rendering must not throw AND the card must show zh strings.
    await tester.runAsync(() async {
      final png = await renderDayReport(
        date: '2026-08-02',
        meals: [
          ReportMeal(meal: _meal(1, '8:05 AM', 'Soy milk and youtiao', 380)),
        ],
        typicalKcal: 2020,
        locale: const Locale('zh'),
      );
      expect(png.sublist(0, 8),
          Uint8List.fromList([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]));
    });
    // Content contract, checked where text is findable: the same card
    // under a zh app scope.
    tester.view.physicalSize = const Size(420, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(MaterialApp(
      locale: const Locale('zh'),
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      supportedLocales: AppLocalizations.supportedLocales,
      home: SingleChildScrollView(
        child: DayReportCard(
          date: '2026-08-02',
          meals: [
            ReportMeal(
                meal: _meal(1, '8:05 AM', 'Soy milk and youtiao', 380)),
          ],
          typicalKcal: 2020,
        ),
      ),
    ));
    expect(find.text('今日饮食'), findsOneWidget);
    expect(find.text('380 千卡'), findsOneWidget);
    expect(find.text('由筷拍记录'), findsOneWidget);
    // Dates and clocks localize too (user-reported 2026-08-03: History
    // weekdays stayed English in zh) — the report shares the helpers.
    expect(find.text('8月2日 星期日'), findsOneWidget);
    expect(find.text('08:05'), findsOneWidget,
        reason: 'zh clocks are 24-hour, never AM/PM');
    expect(find.text('8:05 AM'), findsNothing);
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

    // TODAY's date, computed — a pinned date turns this into a time bomb
    // the first midnight after it's written (it detonated 2026-08-03).
    dao.put(_meal(0, '8:05 AM', 'Soy milk', 110,
        date: isoDate(DateTime.now())));
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: TodayScreen(
                key: UniqueKey(), dao: dao, executor: FakeExecutor()))));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('shareDayButton')), findsOneWidget);
  });
}
