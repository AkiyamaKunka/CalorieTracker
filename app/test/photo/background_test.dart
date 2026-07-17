// Background backfill wiring (spec §6.4): task-handler behavior, lookback
// input clamping (§8: 1–30, default 2), scheduling floor (>=15 min), and the
// iOS no-op delta (§6).
import 'package:calorie_tracker/services/photo/background.dart';
import 'package:flutter_test/flutter_test.dart';

class FakeScheduler implements BackgroundScheduler {
  final List<String> calls = [];
  Function? initializedDispatcher;
  Duration? registeredFrequency;
  Map<String, dynamic>? registeredInput;
  String? registeredUniqueName;
  String? registeredTaskName;

  @override
  Future<void> initialize(Function dispatcher) async {
    calls.add('initialize');
    initializedDispatcher = dispatcher;
  }

  @override
  Future<void> registerPeriodic(String uniqueName, String taskName,
      {required Duration frequency, Map<String, dynamic>? inputData}) async {
    calls.add('register');
    registeredUniqueName = uniqueName;
    registeredTaskName = taskName;
    registeredFrequency = frequency;
    registeredInput = inputData;
  }

  @override
  Future<void> cancelByUniqueName(String uniqueName) async {
    calls.add('cancel:$uniqueName');
  }
}

void main() {
  tearDown(() {
    headlessBackfill = null;
    debugSchedulerOverride = null;
    debugIsAndroidOverride = null;
  });

  group('lookbackDaysFromInput', () {
    test('numeric values clamp to 1–30', () {
      expect(lookbackDaysFromInput(2), 2);
      expect(lookbackDaysFromInput(0), 1);
      expect(lookbackDaysFromInput(99), 30);
      expect(lookbackDaysFromInput(7.9), 7);
    });
    test('junk falls back to the default 2', () {
      expect(lookbackDaysFromInput(null), 2);
      expect(lookbackDaysFromInput('yesterday'), 2);
      expect(lookbackDaysFromInput(double.nan), 2);
    });
  });

  group('handleBackgroundTask', () {
    test('runs the wired backfill with the clamped lookback', () async {
      final seen = <int>[];
      headlessBackfill = (days) async {
        seen.add(days);
        return true;
      };
      final ok = await handleBackgroundTask(
          photoBackfillTaskName, {'lookbackDays': 99});
      expect(ok, isTrue);
      expect(seen, [30]);
    });

    test('missing input uses the default window', () async {
      final seen = <int>[];
      headlessBackfill = (days) async {
        seen.add(days);
        return true;
      };
      await handleBackgroundTask(photoBackfillTaskName, null);
      expect(seen, [2]);
    });

    test('unknown task name: success without invoking the runner', () async {
      var ran = false;
      headlessBackfill = (_) async {
        ran = true;
        return true;
      };
      expect(await handleBackgroundTask('someOtherTask', null), isTrue);
      expect(ran, isFalse);
    });

    test('unwired runner: success (never a retry-storm)', () async {
      expect(await handleBackgroundTask(photoBackfillTaskName, null), isTrue);
    });

    test('runner exception is contained and reported as success (§6.5: '
        'ledger keeps failed rows)', () async {
      headlessBackfill = (_) async => throw StateError('scan blew up');
      expect(await handleBackgroundTask(photoBackfillTaskName, null), isTrue);
    });
  });

  group('enableBackgroundScan', () {
    test('registers one periodic task with clamped frequency and lookback',
        () async {
      final scheduler = FakeScheduler();
      debugSchedulerOverride = scheduler;
      debugIsAndroidOverride = true;

      await enableBackgroundScan(true,
          lookbackDays: 99, frequency: const Duration(minutes: 1));

      expect(scheduler.calls,
          ['initialize', 'cancel:$photoBackfillUniqueName', 'register']);
      expect(scheduler.registeredUniqueName, photoBackfillUniqueName);
      expect(scheduler.registeredTaskName, photoBackfillTaskName);
      expect(scheduler.registeredFrequency,
          minBackgroundFrequency); // WorkManager >=15 min floor
      expect(scheduler.registeredInput, {'lookbackDays': 30}); // §8 clamp
    });

    test('frequency above the floor is kept as-is', () async {
      final scheduler = FakeScheduler();
      debugSchedulerOverride = scheduler;
      debugIsAndroidOverride = true;

      await enableBackgroundScan(true, frequency: const Duration(hours: 6));
      expect(scheduler.registeredFrequency, const Duration(hours: 6));
      expect(scheduler.registeredInput, {'lookbackDays': 2});
    });

    test('off cancels the unique task and registers nothing', () async {
      final scheduler = FakeScheduler();
      debugSchedulerOverride = scheduler;
      debugIsAndroidOverride = true;

      await enableBackgroundScan(false);
      expect(scheduler.calls, ['cancel:$photoBackfillUniqueName']);
    });

    test('iOS: complete no-op (spec §6 delta: share/picker only)', () async {
      final scheduler = FakeScheduler();
      debugSchedulerOverride = scheduler;
      debugIsAndroidOverride = false;

      await enableBackgroundScan(true);
      await enableBackgroundScan(false);
      expect(scheduler.calls, isEmpty);
    });

    test('a custom dispatcher is what gets initialized (integrator wiring)',
        () async {
      final scheduler = FakeScheduler();
      debugSchedulerOverride = scheduler;
      debugIsAndroidOverride = true;
      void myDispatcher() {}

      await enableBackgroundScan(true, dispatcher: myDispatcher);
      expect(scheduler.initializedDispatcher, same(myDispatcher));
    });
  });
}
