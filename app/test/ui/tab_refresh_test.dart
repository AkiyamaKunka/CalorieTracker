// Stale-list regression (real report 2026-07-26): a meal SAVED while the app
// is open — by the watcher, the launch/resume catch-up, or the coverage
// screen — must appear without the user discovering pull-to-refresh. The
// screens live in an IndexedStack and are built once, so both the
// tab-selection reload and the save signal are load-bearing.
import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/app.dart';
import 'package:calorie_tracker/ui/refresh_signal.dart';
import 'package:calorie_tracker/ui/services.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

Meal mealAt(int id, String time, String desc, num kcal) => Meal(
      id: id,
      date: DateTime.now().toIso8601String().substring(0, 10),
      time: time,
      timestamp: DateTime.now().toIso8601String(),
      source: 'app_watch',
      imageHash: 'h$id',
      analysis: {
        'is_food': true,
        'meal_description': desc,
        'total_calories': kcal,
      },
    );

UiServices services(FakeDao dao) => UiServices(
      dao: dao,
      analyzer: FakeAnalyzer(),
      executor: FakeExecutor(),
      settings: FakeSettings(),
      picker: FakePicker(),
      requestPhotoPermission: () async => true,
    );

void main() {
  testWidgets('a meal saved while Today is open appears on the save signal',
      (tester) async {
    final dao = FakeDao()..seed(mealAt(1, '05:14 PM', 'Soymilk', 135));
    await tester.pumpWidget(
        CalorieTrackerApp(services: services(dao)));
    await tester.pumpAndSettle();
    expect(find.textContaining('Soymilk'), findsOneWidget);
    expect(find.textContaining('latte'), findsNothing);

    // The pipeline saves the 07:40 latte (catch-up) behind the visible UI.
    dao.seed(mealAt(2, '07:40 AM', 'Iced protein latte', 210));
    signalMealsChanged();
    await tester.pumpAndSettle();

    expect(find.textContaining('latte'), findsOneWidget,
        reason: 'the list must not keep showing pre-save data');
    expect(find.textContaining('345'), findsOneWidget); // totals recomputed
  });

  testWidgets('re-selecting the Today tab re-queries', (tester) async {
    final dao = FakeDao()..seed(mealAt(1, '05:14 PM', 'Soymilk', 135));
    await tester.pumpWidget(CalorieTrackerApp(services: services(dao)));
    await tester.pumpAndSettle();

    // A save that did NOT signal (e.g. the WorkManager isolate wrote it while
    // this process was frozen) plus a tab round-trip.
    dao.seed(mealAt(2, '07:40 AM', 'Iced protein latte', 210));
    // Scope to the nav bar: 'Today' is also the app-bar title.
    Finder navTab(String label) => find.descendant(
        of: find.byType(NavigationBar), matching: find.text(label));
    await tester.tap(navTab('History'));
    await tester.pumpAndSettle();
    await tester.tap(navTab('Today'));
    await tester.pumpAndSettle();

    expect(find.textContaining('latte'), findsOneWidget);
  });
}
