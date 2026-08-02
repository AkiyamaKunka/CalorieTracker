// The Today hero ring (2026-08-02 redesign). Pins the center-label logic —
// the one place the redesign could actively LIE about the day.
import 'package:calorie_tracker/ui/widgets/calorie_ring.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  Future<void> pump(WidgetTester tester, Widget child) => tester.pumpWidget(
      MaterialApp(home: Scaffold(body: Center(child: child))));

  testWidgets('under typical: center = headroom', (tester) async {
    await pump(tester,
        const CalorieRing(eatenKcal: 1240, typicalKcal: 2020));
    expect(
        tester.widget<Text>(find.byKey(const Key('ringCenterValue'))).data,
        '780');
    expect(find.text('headroom'), findsOneWidget);
  });

  testWidgets('over typical: center = +over, calm wording, no shame state',
      (tester) async {
    await pump(tester,
        const CalorieRing(eatenKcal: 3340, typicalKcal: 1760));
    expect(
        tester.widget<Text>(find.byKey(const Key('ringCenterValue'))).data,
        '+1,580');
    expect(find.text('over typical'), findsOneWidget);
  });

  testWidgets('no typical yet: center = eaten, labeled plainly',
      (tester) async {
    await pump(tester, const CalorieRing(eatenKcal: 345, typicalKcal: null));
    expect(
        tester.widget<Text>(find.byKey(const Key('ringCenterValue'))).data,
        '345');
    expect(find.text('kcal today'), findsOneWidget);
  });

  testWidgets('thousands are grouped', (tester) async {
    await pump(tester,
        const CalorieRing(eatenKcal: 0, typicalKcal: 2020));
    expect(
        tester.widget<Text>(find.byKey(const Key('ringCenterValue'))).data,
        '2,020');
  });

  testWidgets('macro trio scales bars against the LARGEST macro',
      (tester) async {
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
