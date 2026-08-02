// The Body tab (2026-08-02): weight history + waist/chest/hip. These pin
// the page's contract: latest values with honest deltas, a chart only when
// there is a trend to draw, prefilled edit (so the full-row upsert can't
// eat data), validation on the SHARED weight bounds, and delete confirm.
import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/screens/body_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

void main() {
  final clock = DateTime(2026, 8, 2, 10);

  Future<FakeDao> pump(WidgetTester tester,
      {Map<String, double>? weights,
      List<BodyMeasurements> measurements = const []}) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final dao = FakeDao();
    weights?.forEach((date, kg) =>
        dao.weightsByDate[date] = WeightEntry(date, kg, source: 'manual'));
    for (final m in measurements) {
      dao.measurementsByDate[m.date] = m;
    }
    // UniqueKey: a second pump in the same test must get a FRESH State
    // (same-type widgets at the same position reuse state, skipping
    // initState and therefore the new dao's load).
    await tester.pumpWidget(MaterialApp(
        home: BodyScreen(key: UniqueKey(), dao: dao, clock: () => clock)));
    await tester.pumpAndSettle();
    return dao;
  }

  testWidgets('empty state invites logging and mentions the chat path',
      (tester) async {
    await pump(tester);
    expect(find.byKey(const Key('bodyEmpty')), findsOneWidget);
    expect(find.byKey(const Key('logBodyButton')), findsOneWidget);
  });

  testWidgets('latest weight is the headline; delta is against the previous '
      'entry', (tester) async {
    await pump(tester, weights: {'2026-07-28': 82.4, '2026-08-02': 81.6});
    expect(find.byKey(const Key('bodyWeightHeadline')), findsOneWidget);
    expect(find.text('81.6'), findsOneWidget);
    final delta = tester
        .widget<Text>(find.descendant(
            of: find.byKey(const Key('bodyWeightDelta')),
            matching: find.byType(Text)))
        .data!;
    expect(delta, contains('0.8'));
    expect(delta, contains('▼'), reason: '81.6 < 82.4 — direction in text');
  });

  testWidgets('one weigh-in renders no chart; two render the trend',
      (tester) async {
    await pump(tester, weights: {'2026-08-02': 81.6});
    expect(find.byKey(const Key('weightTrendChart')), findsNothing,
        reason: 'a single point is not a trend');
    await pump(tester, weights: {'2026-07-28': 82.4, '2026-08-02': 81.6});
    expect(find.byKey(const Key('weightTrendChart')), findsOneWidget);
  });

  testWidgets('measurements card shows per-metric latest even when days are '
      'sparse', (tester) async {
    await pump(tester, measurements: [
      const BodyMeasurements('2026-07-30', waistCm: 84, chestCm: 100),
      const BodyMeasurements('2026-08-02', waistCm: 83.5), // chest not taken
    ]);
    expect(find.text('83.5 cm'), findsOneWidget);
    expect(find.text('100 cm'), findsOneWidget,
        reason: 'a day without chest must not blank the chest series');
  });

  testWidgets('logging weight through the sheet saves with source manual '
      'and refreshes the page', (tester) async {
    final dao = await pump(tester);
    await tester.tap(find.byKey(const Key('logBodyButton')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('bodyWeightField')), '81.6');
    await tester.tap(find.byKey(const Key('bodySheetSave')));
    await tester.pumpAndSettle();

    expect(dao.weightsByDate['2026-08-02']?.kg, 81.6);
    expect(dao.weightsByDate['2026-08-02']?.source, 'manual');
    expect(find.text('81.6'), findsOneWidget, reason: 'page reloaded');
    expect(find.byKey(const Key('bodyEmpty')), findsNothing);
  });

  testWidgets('a weight outside the SHARED parity bounds is refused with '
      'the bounds named', (tester) async {
    final dao = await pump(tester);
    await tester.tap(find.byKey(const Key('logBodyButton')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('bodyWeightField')), '500');
    await tester.tap(find.byKey(const Key('bodySheetSave')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('bodySheetError')), findsOneWidget);
    expect(find.textContaining('300'), findsOneWidget,
        reason: 'the message names the real bound (shared weightMaxKg)');
    expect(dao.weightsByDate, isEmpty);
  });

  testWidgets('an empty sheet save is refused', (tester) async {
    final dao = await pump(tester);
    await tester.tap(find.byKey(const Key('logBodyButton')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('bodySheetSave')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('bodySheetError')), findsOneWidget);
    expect(dao.weightsByDate, isEmpty);
    expect(dao.measurementsByDate, isEmpty);
  });

  testWidgets('editing a day PREFILLS, so an untouched field survives the '
      'full-row upsert', (tester) async {
    final dao = await pump(tester, measurements: [
      const BodyMeasurements('2026-08-02', waistCm: 84, chestCm: 100),
    ]);
    await tester.tap(find.byKey(const Key('bodyDay2026-08-02')));
    await tester.pumpAndSettle();
    // Change only the waist; chest must carry through via the prefill.
    await tester.enterText(find.byKey(const Key('bodyWaistField')), '83.5');
    await tester.tap(find.byKey(const Key('bodySheetSave')));
    await tester.pumpAndSettle();

    final saved = dao.measurementsByDate['2026-08-02']!;
    expect(saved.waistCm, 83.5);
    expect(saved.chestCm, 100, reason: 'prefill preserved the untouched field');
  });

  testWidgets('clearing a prefilled weight on edit deletes that weigh-in',
      (tester) async {
    final dao = await pump(tester,
        weights: {'2026-08-02': 81.6},
        measurements: [const BodyMeasurements('2026-08-02', waistCm: 84)]);
    await tester.tap(find.byKey(const Key('bodyDay2026-08-02')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('bodyWeightField')), '');
    await tester.tap(find.byKey(const Key('bodySheetSave')));
    await tester.pumpAndSettle();
    expect(dao.weightsByDate, isEmpty,
        reason: 'a cleared field really clears');
    expect(dao.measurementsByDate['2026-08-02']?.waistCm, 84);
  });

  testWidgets('delete asks first, then removes both tables for the day',
      (tester) async {
    final dao = await pump(tester,
        weights: {'2026-08-02': 81.6},
        measurements: [const BodyMeasurements('2026-08-02', waistCm: 84)]);
    await tester.tap(find.byIcon(Icons.delete_outline));
    await tester.pumpAndSettle();
    expect(dao.weightsByDate, isNotEmpty, reason: 'nothing until confirmed');
    await tester.tap(find.byKey(const Key('confirmDeleteBodyDay')));
    await tester.pumpAndSettle();
    expect(dao.weightsByDate, isEmpty);
    expect(dao.measurementsByDate, isEmpty);
    expect(find.byKey(const Key('bodyEmpty')), findsOneWidget);
  });

  testWidgets('history rows list newest first with the values inline',
      (tester) async {
    await pump(tester,
        weights: {'2026-07-28': 82.4, '2026-08-02': 81.6},
        measurements: [const BodyMeasurements('2026-08-02', waistCm: 84)]);
    final rows = tester
        .widgetList<ListTile>(find.byType(ListTile))
        .map((t) => (t.title as Text).data)
        .toList();
    expect(rows, ['2026-08-02', '2026-07-28']);
    expect(find.textContaining('W 84'), findsOneWidget);
  });
}
