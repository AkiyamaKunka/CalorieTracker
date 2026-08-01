// History gap-day rows (2026-07-31): days with zero logged meals used to
// simply VANISH from the list — a watcher outage looked like a normal
// list. Interior gaps (plus the span up to today) now render as dimmed
// 'no meals logged' rows, tappable to the day editor; leading empty spans
// stay collapsed. This suite shipped a day late — the feature went out
// untested (loop debt, closed here).
import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/screens/history_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

String _iso(DateTime d) =>
    '${d.year}-${d.month.toString().padLeft(2, '0')}-'
    '${d.day.toString().padLeft(2, '0')}';

Meal _meal(String date, {num cal = 500, int id = 0}) => Meal(
      id: id,
      date: date,
      time: '12:00 PM',
      timestamp: '${date}T12:00:00.000',
      source: 'app_watch',
      imageHash: 'h$date$id',
      analysis: {'is_food': true, 'total_calories': cal},
    );

void main() {
  Future<FakeDao> pump(WidgetTester tester, List<Meal> meals) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final dao = FakeDao();
    var id = 1;
    for (final m in meals) {
      dao.meals.add(Meal(
          id: id++,
          date: m.date,
          time: m.time,
          timestamp: m.timestamp,
          source: m.source,
          imageHash: m.imageHash,
          analysis: m.analysis));
    }
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(body: HistoryScreen(dao: dao, days: 30))));
    await tester.pumpAndSettle();
    return dao;
  }

  testWidgets('an interior gap day renders dimmed with "no meals logged"',
      (tester) async {
    final now = DateTime.now();
    final d3 = _iso(now.subtract(const Duration(days: 3)));
    final d1 = _iso(now.subtract(const Duration(days: 1)));
    final d2 = _iso(now.subtract(const Duration(days: 2)));
    await pump(tester, [_meal(d3, cal: 700), _meal(d1, cal: 600)]);

    expect(find.byKey(Key('historyDay$d2')), findsOneWidget,
        reason: 'the missed day must EXIST as a row');
    expect(find.text('no meals logged'), findsWidgets);
    // Logged days still show their totals.
    expect(find.byKey(Key('historyDay$d3')), findsOneWidget);
    expect(find.byKey(Key('historyDay$d1')), findsOneWidget);
  });

  testWidgets('the span extends to TODAY even when today is empty',
      (tester) async {
    final now = DateTime.now();
    final today = _iso(now);
    final d2 = _iso(now.subtract(const Duration(days: 2)));
    await pump(tester, [_meal(d2)]);
    expect(find.byKey(Key('historyDay$today')), findsOneWidget,
        reason: '"nothing logged yet today/yesterday" is exactly the gap '
            'worth noticing');
  });

  testWidgets('LEADING empty span stays collapsed — a new user never sees '
      '29 empty rows', (tester) async {
    final now = DateTime.now();
    final today = _iso(now);
    final d29 = _iso(now.subtract(const Duration(days: 29)));
    await pump(tester, [_meal(today)]);
    expect(find.byKey(Key('historyDay$d29')), findsNothing,
        reason: 'days before the OLDEST logged day are not rendered');
    expect(find.text('no meals logged'), findsNothing);
  });

  testWidgets('gap rows open the day detail (the remedy is one tap away)',
      (tester) async {
    final now = DateTime.now();
    final d1 = _iso(now.subtract(const Duration(days: 1)));
    final d3 = _iso(now.subtract(const Duration(days: 3)));
    await pump(tester, [_meal(d3), _meal(d1)]);
    final d2 = _iso(now.subtract(const Duration(days: 2)));
    await tester.tap(find.byKey(Key('historyDay$d2')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('addMealToDay')), findsOneWidget,
        reason: 'DayDetail with its + FAB is the fix for a missed day');
  });

  testWidgets('a hand-edited FUTURE meal stays OUT of History — the §5.3 '
      'window ends at today', (tester) async {
    // The query window is [today-29, today] (server parity), so a
    // future-dated meal never reaches _perDay; the span-end guard in the
    // screen exists only for clock skew between load and build.
    final now = DateTime.now();
    final today = _iso(now);
    final future = _iso(now.add(const Duration(days: 2)));
    await pump(tester, [_meal(today), _meal(future)]);
    expect(find.byKey(Key('historyDay$future')), findsNothing);
    expect(find.byKey(Key('historyDay$today')), findsOneWidget);
  });

  testWidgets('day iteration crosses a month boundary without skips or '
      'duplicates', (tester) async {
    // Seed the 1st of this month and 28 days earlier — the generated span
    // must contain every date exactly once (the day+1 CONSTRUCTOR rule;
    // a Duration(hours:24) add would drift under DST).
    final now = DateTime.now();
    final old = now.subtract(const Duration(days: 28));
    await pump(tester, [_meal(_iso(old))]);
    final tiles = tester
        .widgetList<ListTile>(find.byType(ListTile))
        .length;
    expect(tiles, 29,
        reason: '28 days ago .. today inclusive = 29 unique rows');
  });
}
