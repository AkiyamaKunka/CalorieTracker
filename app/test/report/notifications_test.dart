/// Tests for ReportNotifier's dependency-free daily scheduling (spec §5):
/// hh:mm parsing, next-occurrence math, the reschedule-after-fire pattern,
/// re-scheduling replacing the old slot, and meal-card presentation.
library;

import 'package:calorie_tracker/services/report/notifications.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('parseHhmm', () {
    test('accepts 24h HH:mm', () {
      expect(ReportNotifier.parseHhmm('08:00'), (hour: 8, minute: 0));
      expect(ReportNotifier.parseHhmm('23:59'), (hour: 23, minute: 59));
      expect(ReportNotifier.parseHhmm('7:05'), (hour: 7, minute: 5));
      expect(ReportNotifier.parseHhmm(' 09:30 '), (hour: 9, minute: 30));
    });

    test('rejects out-of-range and malformed input', () {
      for (final bad in ['24:00', '12:60', '8am', '', '08:0', '08-00', '8:5']) {
        expect(() => ReportNotifier.parseHhmm(bad), throwsArgumentError,
            reason: bad);
      }
    });
  });

  group('nextDailyOccurrence', () {
    final now = DateTime(2026, 7, 17, 8, 0);

    test('later today when the slot is still ahead', () {
      expect(
          ReportNotifier.nextDailyOccurrence((hour: 9, minute: 30), now),
          DateTime(2026, 7, 17, 9, 30));
    });

    test('tomorrow when the slot equals now (strictly-after rule)', () {
      expect(
          ReportNotifier.nextDailyOccurrence((hour: 8, minute: 0), now),
          DateTime(2026, 7, 18, 8, 0));
    });

    test('tomorrow when the slot already passed, across month ends', () {
      expect(
          ReportNotifier.nextDailyOccurrence(
              (hour: 6, minute: 0), DateTime(2026, 7, 31, 7, 0)),
          DateTime(2026, 8, 1, 6, 0));
    });
  });

  testWidgets('scheduleDaily fires at the slot and re-arms for the next day',
      (tester) async {
    var now = DateTime(2026, 7, 17, 7, 0);
    final fired = <(int, String, String)>[];
    final notifier = ReportNotifier(
      clock: () => now,
      presenter: (id, title, body) async => fired.add((id, title, body)),
      dailyBody: (slotDate) async => 'report body for $slotDate',
    );

    await notifier.scheduleDaily('08:00');
    expect(notifier.nextDailyFire, DateTime(2026, 7, 17, 8, 0));

    await tester.pump(const Duration(minutes: 59));
    expect(fired, isEmpty);

    now = DateTime(2026, 7, 17, 8, 0);
    await tester.pump(const Duration(minutes: 1));
    expect(fired.length, 1);
    expect(fired.single.$1, ReportNotifier.dailyReportNotificationId);
    expect(fired.single.$2, '📊 Daily Calorie Report');
    // The body is built FOR THE ARMED SLOT's date — a late fire (overnight
    // suspension) must report the intended day, not the fresh morning.
    expect(fired.single.$3, 'report body for ${DateTime(2026, 7, 17, 8, 0)}');
    // Reschedule-after-fire: the next slot is armed without a new call.
    expect(notifier.nextDailyFire, DateTime(2026, 7, 18, 8, 0));

    now = DateTime(2026, 7, 18, 8, 0);
    await tester.pump(const Duration(hours: 24));
    expect(fired.length, 2);

    notifier.cancelDaily();
    expect(notifier.nextDailyFire, isNull);
    await tester.pump(const Duration(hours: 48));
    expect(fired.length, 2);
  });

  testWidgets('re-scheduling replaces the previous slot', (tester) async {
    var now = DateTime(2026, 7, 17, 7, 0);
    final fired = <(int, String, String)>[];
    final notifier = ReportNotifier(
      clock: () => now,
      presenter: (id, title, body) async => fired.add((id, title, body)),
    );

    await notifier.scheduleDaily('08:00');
    await notifier.scheduleDaily('09:00');
    expect(notifier.nextDailyFire, DateTime(2026, 7, 17, 9, 0));

    now = DateTime(2026, 7, 17, 8, 30);
    await tester.pump(const Duration(minutes: 90));
    expect(fired, isEmpty, reason: 'the 08:00 timer must be cancelled');

    now = DateTime(2026, 7, 17, 9, 0);
    await tester.pump(const Duration(minutes: 30));
    expect(fired.length, 1);

    notifier.cancelDaily();
  });

  testWidgets('an invalid reschedule leaves the running schedule intact',
      (tester) async {
    var now = DateTime(2026, 7, 17, 7, 0);
    final fired = <(int, String, String)>[];
    final notifier = ReportNotifier(
      clock: () => now,
      presenter: (id, title, body) async => fired.add((id, title, body)),
    );

    await notifier.scheduleDaily('08:00');
    await expectLater(notifier.scheduleDaily('25:99'), throwsArgumentError);
    expect(notifier.nextDailyFire, DateTime(2026, 7, 17, 8, 0));

    now = DateTime(2026, 7, 17, 8, 0);
    await tester.pump(const Duration(hours: 1));
    expect(fired.length, 1);

    notifier.cancelDaily();
  });

  testWidgets('a throwing dailyBody degrades to the fallback body and re-arms',
      (tester) async {
    var now = DateTime(2026, 7, 17, 7, 0);
    final fired = <(int, String, String)>[];
    final notifier = ReportNotifier(
      clock: () => now,
      presenter: (id, title, body) async => fired.add((id, title, body)),
      dailyBody: (_) async => throw StateError('builder broke'),
    );

    await notifier.scheduleDaily('08:00');
    now = DateTime(2026, 7, 17, 8, 0);
    await tester.pump(const Duration(hours: 1));
    expect(fired.length, 1);
    expect(fired.single.$3, 'Your daily calorie report is ready.');
    expect(notifier.nextDailyFire, DateTime(2026, 7, 18, 8, 0));

    notifier.cancelDaily();
  });

  test('showMealCard presents with distinct stacking ids', () async {
    final fired = <(int, String, String)>[];
    final notifier = ReportNotifier(
      presenter: (id, title, body) async => fired.add((id, title, body)),
    );
    await notifier.init(); // presenter mode: must not touch the plugin
    await notifier.showMealCard('Meal logged', '~450 kcal');
    await notifier.showMealCard('Meal logged', '~620 kcal');
    expect(fired.length, 2);
    expect(fired[0].$2, 'Meal logged');
    expect(fired[0].$3, '~450 kcal');
    expect(fired[1].$3, '~620 kcal');
    expect(fired[0].$1, isNot(fired[1].$1));
    expect(fired.map((f) => f.$1),
        isNot(contains(ReportNotifier.dailyReportNotificationId)));
  });
}
