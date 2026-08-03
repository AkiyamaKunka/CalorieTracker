// Imperial units (user request 2026-08-03): DISPLAY-only conversion for
// body data. The contract these pin: storage stays metric bit-for-bit, the
// English UI can render/enter lb+in, the Chinese UI is ALWAYS metric and
// never offers the choice, and an untouched prefill survives an imperial
// round-trip without drift.
import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/app.dart';
import 'package:calorie_tracker/ui/screens/body_screen.dart';
import 'package:calorie_tracker/ui/units.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

void main() {
  test('conversions are exact inverses at the definition constants', () {
    expect(kgToLb(1), closeTo(2.2046226, 1e-6));
    expect(lbToKg(kgToLb(81.6)), closeTo(81.6, 1e-12));
    expect(cmToIn(2.54), closeTo(1, 1e-12));
    expect(inToCm(cmToIn(94.5)), closeTo(94.5, 1e-12));
  });

  final clock = DateTime(2026, 8, 2, 10);

  Future<FakeDao> pump(WidgetTester tester,
      {Map<String, double>? weights,
      List<BodyMeasurements> measurements = const [],
      FakeSettings? settings}) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final dao = FakeDao();
    weights?.forEach((date, kg) =>
        dao.weightsByDate[date] = WeightEntry(date, kg, source: 'manual'));
    for (final m in measurements) {
      dao.measurementsByDate[m.date] = m;
    }
    await tester.pumpWidget(MaterialApp(
        home: BodyScreen(
            key: UniqueKey(),
            dao: dao,
            settings: settings ?? (FakeSettings()..units = 'imperial'),
            clock: () => clock)));
    await tester.pumpAndSettle();
    return dao;
  }

  testWidgets('imperial renders lb and inches everywhere on the page',
      (tester) async {
    await pump(tester,
        weights: {'2026-07-28': 82.4, '2026-08-02': 81.6},
        measurements: [
          BodyMeasurements('2026-08-02', waistCm: 94.5),
        ]);
    // 81.6 kg = 179.9 lb; 94.5 cm = 37.2 in.
    expect(
        tester
            .widget<Text>(find.byKey(const Key('bodyWeightHeadline')))
            .data,
        '179.9');
    expect(find.text('lb'), findsOneWidget);
    expect(find.textContaining('37.2 in'), findsOneWidget);
    // The history row speaks lb too, and no kg leaks anywhere.
    expect(find.textContaining('179.9 lb'), findsOneWidget);
    expect(find.textContaining(' kg'), findsNothing);
    expect(find.textContaining(' cm'), findsNothing);
  });

  testWidgets('imperial input converts to metric for storage and bounds',
      (tester) async {
    final dao = await pump(tester);
    await tester.tap(find.byKey(const Key('logBodyButton')));
    await tester.pumpAndSettle();
    expect(find.text('lb'), findsOneWidget); // field suffix
    expect(find.text('in'), findsNWidgets(3));

    // 180 lb → 81.6 kg stored (NOT 180).
    await tester.enterText(
        find.byKey(const Key('bodyWeightField')), '180');
    await tester.tap(find.byKey(const Key('bodySheetSave')));
    await tester.pumpAndSettle();
    expect(dao.weightsByDate['2026-08-02']!.kg, closeTo(81.65, 0.01));

    // Out-of-bounds shows the bounds in POUNDS (shared 30–300 kg).
    await tester.tap(find.byKey(const Key('logBodyButton')));
    await tester.pumpAndSettle();
    await tester.enterText(
        find.byKey(const Key('bodyWeightField')), '20');
    await tester.tap(find.byKey(const Key('bodySheetSave')));
    await tester.pumpAndSettle();
    final err = tester
        .widget<Text>(find.byKey(const Key('bodySheetError')))
        .data!;
    expect(err, contains('66.1'));
    expect(err, contains('661.4'));

    // The ADVERTISED endpoint must be typeable: 66.1 lb rounds into the
    // display bounds, saves, and clamps to the exact parity floor.
    await tester.enterText(
        find.byKey(const Key('bodyWeightField')), '66.1');
    await tester.tap(find.byKey(const Key('bodySheetSave')));
    await tester.pumpAndSettle();
    expect(dao.weightsByDate['2026-08-02']!.kg, 30.0);
  });

  testWidgets('the imperial empty state teaches an lb chat example',
      (tester) async {
    await pump(tester);
    expect(find.textContaining('180 lb'), findsOneWidget);
    expect(find.textContaining('81.6 kg'), findsNothing);
  });

  testWidgets('an untouched imperial prefill saves the stored kg '
      'bit-for-bit (no round-trip drift)', (tester) async {
    final dao = await pump(tester, weights: {'2026-08-02': 81.6});
    await tester.tap(find.byKey(const Key('logBodyButton')));
    await tester.pumpAndSettle();
    // Prefill shows 179.9 (rounded lb). Saving it unchanged must keep
    // 81.6 exactly — not lbToKg(179.9) = 81.59.
    expect(
        tester
            .widget<TextField>(find.byKey(const Key('bodyWeightField')))
            .controller!
            .text,
        '179.9');
    await tester.tap(find.byKey(const Key('bodySheetSave')));
    await tester.pumpAndSettle();
    expect(dao.weightsByDate['2026-08-02']!.kg, 81.6);
  });

  testWidgets('metric setting renders kg/cm unchanged', (tester) async {
    await pump(tester,
        weights: {'2026-08-02': 81.6},
        settings: FakeSettings()); // units defaults to metric
    expect(
        tester
            .widget<Text>(find.byKey(const Key('bodyWeightHeadline')))
            .data,
        '81.6');
    expect(find.text('kg'), findsOneWidget);
  });

  testWidgets('the Chinese UI is metric even with imperial stored, and '
      'hides the Units row', (tester) async {
    final settings = FakeSettings()
      ..appLanguage = 'zh'
      ..units = 'imperial';
    final dao = FakeDao();
    dao.weightsByDate['2026-08-02'] =
        WeightEntry('2026-08-02', 81.6, source: 'manual');
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(CalorieTrackerApp(
        services: makeServices(dao: dao, settings: settings)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('身体'));
    await tester.pumpAndSettle();
    expect(find.text('kg'), findsOneWidget);
    expect(find.text('lb'), findsNothing);
    await tester.tap(find.text('设置'));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.byKey(const Key('languageRow')));
    expect(find.byKey(const Key('unitsRow')), findsNothing);
  });

  testWidgets('the English UI offers the Units row and the choice persists',
      (tester) async {
    final settings = FakeSettings();
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(CalorieTrackerApp(
        services: makeServices(settings: settings)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Settings'));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.byKey(const Key('unitsRow')));
    await tester.tap(find.byKey(const Key('unitsRow')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('units-imperial')));
    await tester.pumpAndSettle();
    expect(settings.units, 'imperial');
  });
}
