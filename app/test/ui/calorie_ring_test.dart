// The Today hero ring (2026-08-02 redesign, reconciled after review).
// Pins the arithmetic's honesty: budget = typical + burn, and the center
// number IS what the visible rows produce — the one place the redesign
// could actively lie about the day.
import 'package:calorie_tracker/ui/widgets/calorie_ring.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  Future<void> pump(WidgetTester tester, Widget child,
      {double textScale = 1.0}) =>
      tester.pumpWidget(MaterialApp(
          home: MediaQuery(
              data: MediaQueryData(
                  textScaler: TextScaler.linear(textScale)),
              child: Scaffold(body: Center(child: child)))));

  String center(WidgetTester tester) =>
      tester.widget<Text>(find.byKey(const Key('ringCenterValue'))).data!;

  testWidgets('under typical: center = headroom', (tester) async {
    await pump(tester,
        const CalorieRing(eatenKcal: 1240, typicalKcal: 2020));
    expect(center(tester), '780');
    expect(find.text('headroom'), findsOneWidget);
  });

  testWidgets('burn EXTENDS the budget and renames the label — the MFP '
      'equation, reconciled', (tester) async {
    await pump(
        tester,
        const CalorieRing(
            eatenKcal: 1240, typicalKcal: 2020, burnKcal: 88));
    expect(center(tester), '868',
        reason: '2020 + 88 − 1240: the rows must produce this number');
    expect(find.text('left today'), findsOneWidget);
  });

  testWidgets('over budget: +over, caption-matching wording, no shame state',
      (tester) async {
    await pump(tester,
        const CalorieRing(eatenKcal: 3340, typicalKcal: 1760));
    expect(center(tester), '+1,580');
    expect(find.text('above typical'), findsOneWidget,
        reason: 'the spec-pinned caption says "above typical" — one card, '
            'one vocabulary');
  });

  testWidgets('rounding parity: 2000.3 eaten vs 2000 typical is ZERO '
      'headroom, not "+0 over"', (tester) async {
    await pump(tester,
        const CalorieRing(eatenKcal: 2000.3, typicalKcal: 2000));
    expect(center(tester), '0');
    expect(find.text('headroom'), findsOneWidget,
        reason: 'the caption rounds first; the ring must round the same '
            'way or the card contradicts itself');
  });

  testWidgets('no typical yet: center = eaten, labeled plainly',
      (tester) async {
    await pump(tester, const CalorieRing(eatenKcal: 345, typicalKcal: null));
    expect(center(tester), '345');
    expect(find.text('kcal today'), findsOneWidget);
  });

  testWidgets('2x font scale: no overflow, text shrinks to fit the ring',
      (tester) async {
    await pump(
        tester,
        const CalorieRing(eatenKcal: 11240, typicalKcal: 22020),
        textScale: 2.0);
    expect(tester.takeException(), isNull,
        reason: 'FittedBox must absorb the scale; clipped hero text is the '
            'a11y must-fix this pins');
    expect(center(tester), '10,780');
  });

  testWidgets('macro trio fills by CALORIE share (Atwater), matching the '
      'detail chart', (tester) async {
    await pump(
        tester,
        const MacroTrio(
            proteinG: 50,
            carbsG: 100,
            fatG: 0,
            proteinColor: Colors.blue,
            carbsColor: Colors.orange,
            fatColor: Colors.green));
    expect(find.byKey(const Key('macroTrio')), findsOneWidget);
    expect(find.text('P 50g'), findsOneWidget);
    expect(find.text('C 100g'), findsOneWidget);
    expect(find.text('F 0g'), findsOneWidget);
  });
}
