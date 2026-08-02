// Today's Garmin energy-balance line (2026-08-02, spec §9): the active
// burn arrives via the user's OWN server and is strictly cosmetic — it
// must never block, error, or mislead the screen.
import 'package:calorie_tracker/services/garmin_client.dart';
import 'package:calorie_tracker/ui/screens/today_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

void main() {
  Future<void> pump(WidgetTester tester, {GarminDailyFetch? garmin}) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final dao = FakeDao();
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: TodayScreen(
                key: UniqueKey(),
                dao: dao,
                executor: FakeExecutor(),
                garminDaily: garmin))));
    await tester.pumpAndSettle();
  }

  testWidgets('a burn > 0 draws the line with the net math', (tester) async {
    await pump(tester,
        garmin: (date) async => const GarminDaily(
            activeCalories: 512,
            steps: 8000,
            distanceM: 6200,
            activityCount: 1));
    final line =
        tester.widget<Text>(find.byKey(const Key('garminBurnLine'))).data!;
    expect(line, contains('512'));
    expect(line, contains('Garmin'));
    expect(line, contains('net'),
        reason: 'intake minus burn is the point of the line');
  });

  testWidgets('no fetch, no line', (tester) async {
    await pump(tester);
    expect(find.byKey(const Key('garminBurnLine')), findsNothing);
  });

  testWidgets('an unavailable day (null) draws nothing', (tester) async {
    await pump(tester, garmin: (date) async => null);
    expect(find.byKey(const Key('garminBurnLine')), findsNothing);
  });

  testWidgets('a zero-burn day draws nothing — a "~0 kcal" line is noise',
      (tester) async {
    await pump(tester,
        garmin: (date) async => const GarminDaily(
            activeCalories: 0, steps: 0, distanceM: 0, activityCount: 0));
    expect(find.byKey(const Key('garminBurnLine')), findsNothing);
  });

  testWidgets('a fetch that throws leaves the screen intact', (tester) async {
    await pump(tester,
        garmin: (date) async => throw Exception('server down'));
    expect(find.byKey(const Key('todayTotalKcal')), findsOneWidget);
    expect(find.byKey(const Key('garminBurnLine')), findsNothing);
  });

  group('parseGarminDailyBody', () {
    test('maps a full reply', () {
      final d = parseGarminDailyBody(200,
          '{"available":true,"active_calories":512.5,"steps":8000,'
          '"distance_m":6200,"activity_count":2}')!;
      expect(d.activeCalories, 512.5);
      expect(d.steps, 8000);
      expect(d.activityCount, 2);
    });

    test('anything not 200+available is null, never a throw', () {
      expect(parseGarminDailyBody(200, '{"available":false}'), isNull);
      expect(parseGarminDailyBody(401, '{"error":"Unauthorized"}'), isNull);
      expect(parseGarminDailyBody(200, 'not json at all'), isNull);
      expect(parseGarminDailyBody(200, '[]'), isNull);
      expect(
          parseGarminDailyBody(
              200, '{"available":true,"active_calories":"junk"}'),
          isNotNull,
          reason: 'junk numerics coerce to 0, the line simply stays hidden');
    });
  });
}
