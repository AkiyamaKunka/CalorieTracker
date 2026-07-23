// Background-glue wiring (spec §6.4 integrator layer): headless drain
// semantics, watermark handling, guard rails, periodic-job registration,
// and the app-shell resume catch-up. This wiring was MISSING entirely at
// first device use — the watcher only worked while the app was foreground —
// so these tests pin every seam of the fix.
import 'dart:async';
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/photo/background.dart';
import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:calorie_tracker/ui/app.dart';
import 'package:calorie_tracker/ui/background_glue.dart';
import 'package:calorie_tracker/ui/photo_pipeline.dart';
import 'package:calorie_tracker/ui/services.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'fakes.dart';

/// Intake whose backfillScan delivers a scripted batch — mimics the real
/// LibraryPhotoIntake (sink-awaited delivery when attached; stream else).
class EmittingIntake implements PhotoIntake {
  final _controller = StreamController<IntakePhoto>.broadcast();
  List<IntakePhoto> batch = const [];
  List<DateTime?> safeFrontiers = const []; // parallel to batch; pads null
  final List<DateTime?> sinceArgs = [];
  int scans = 0;
  bool throwAfterEmit = false; // scan-abort simulation (library query died)
  DateTime? frontier; // returned from a completed scan
  Future<bool> Function(IntakePhoto, DateTime?)? _sink;

  @override
  Stream<IntakePhoto> get photos => _controller.stream;

  @override
  void attachSink(
          Future<bool> Function(IntakePhoto photo, DateTime? safeFrontier)?
              sink) =>
      _sink = sink;

  @override
  Future<DateTime?> backfillScan(
      {int lookbackDays = 0, DateTime? since}) async {
    scans++;
    sinceArgs.add(since);
    for (var i = 0; i < batch.length; i++) {
      final sink = _sink;
      final safe = i < safeFrontiers.length ? safeFrontiers[i] : null;
      if (sink != null) {
        await sink(batch[i], safe); // backpressured, like the real intake
      } else {
        _controller.add(batch[i]);
      }
    }
    if (throwAfterEmit) throw StateError('library query died mid-scan');
    return frontier;
  }

  @override
  Future<void> start() async {}
  @override
  Future<void> stop() async {}
}

class MemoryKeyStore implements SecureKeyStore {
  final Map<String, String> data = {};
  @override
  Future<String?> read(String key) async => data[key];
  @override
  Future<void> write(String key, String value) async => data[key] = value;
  @override
  Future<void> delete(String key) async => data.remove(key);
}

class RecordingScheduler implements BackgroundScheduler {
  final List<String> calls = [];
  Duration? frequency;
  Map<String, dynamic>? inputData;

  @override
  Future<void> initialize(Function dispatcher) async => calls.add('init');

  @override
  Future<void> registerPeriodic(String uniqueName, String taskName,
      {required Duration frequency, Map<String, dynamic>? inputData}) async {
    calls.add('register:$uniqueName:$taskName');
    this.frequency = frequency;
    this.inputData = inputData;
  }

  @override
  Future<void> cancelByUniqueName(String uniqueName) async =>
      calls.add('cancel:$uniqueName');
}

IntakePhoto photo(int n) => IntakePhoto(
    Uint8List.fromList([n, n, n]), 'asset$n', 'IMG_$n.jpg');

/// FakeDao that fires a callback on each saveMeal — used to observe the
/// watermark state at the moment each photo lands.
class _CheckpointSpyDao extends FakeDao {
  _CheckpointSpyDao(this.onSave);
  final void Function() onSave;
  @override
  Future<int> saveMeal(Meal meal, {IngestionStatus? markStatus}) {
    onSave();
    return super.saveMeal(meal, markStatus: markStatus);
  }
}

Future<AppSettings> settingsWith(
    {String? key,
    bool watcher = true,
    int lookback = 2,
    DateTime? quotaPauseUntil}) async {
  SharedPreferences.setMockInitialValues({
    'settings.watcher_enabled': watcher,
    'settings.lookback_days': lookback,
    if (quotaPauseUntil != null)
      'settings.quota_pause_until': quotaPauseUntil.toIso8601String(),
  });
  final prefs = await SharedPreferences.getInstance();
  final keys = MemoryKeyStore();
  if (key != null) keys.data['gemini_api_key'] = key;
  return AppSettings.load(prefs: prefs, keyStore: keys);
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('drainBackfill', () {
    test('waits for every emitted photo to finish processing', () async {
      final intake = EmittingIntake()..batch = [photo(1), photo(2), photo(3)];
      final dao = FakeDao();
      final analyzer = FakeAnalyzer()
        ..nextPhotoOutcome = const AnalysisOutcome(
            analysis: {'is_food': true, 'food_items': []},
            isFood: true,
            wall: Duration.zero);
      final drain = await drainBackfill(
          intake, PhotoPipeline(dao: dao, analyzer: analyzer));
      expect(drain.scanCompleted, isTrue);
      expect(drain.outcomes, hasLength(3));
      expect(drain.outcomes.map((o) => o.kind),
          everyElement(PhotoOutcomeKind.saved));
      expect(dao.meals, hasLength(3)); // effects landed BEFORE drain returned
    });

    test('a scan abort still processes emitted photos, flags incomplete',
        () async {
      final intake = EmittingIntake()
        ..batch = [photo(1)]
        ..throwAfterEmit = true;
      final dao = FakeDao();
      final analyzer = FakeAnalyzer()
        ..nextPhotoOutcome = const AnalysisOutcome(
            analysis: {'is_food': true, 'food_items': []},
            isFood: true,
            wall: Duration.zero);
      final drain = await drainBackfill(
          intake, PhotoPipeline(dao: dao, analyzer: analyzer));
      expect(drain.scanCompleted, isFalse);
      expect(drain.outcomes, hasLength(1)); // emitted photo NOT abandoned
      expect(dao.meals, hasLength(1));
    });

    test('unsubscribes its listener afterwards', () async {
      final intake = EmittingIntake()..batch = [photo(1)];
      final dao = FakeDao();
      final analyzer = FakeAnalyzer();
      await drainBackfill(intake, PhotoPipeline(dao: dao, analyzer: analyzer));
      final before = dao.ledger.length;
      await intake.backfillScan(); // nobody listening now
      await pumpEventQueue();
      expect(dao.ledger.length, before);
    });
  });

  group('headlessBackfillWith', () {
    late SharedPreferences prefs;

    setUp(() async {
      SharedPreferences.setMockInitialValues({});
      prefs = await SharedPreferences.getInstance();
    });

    test('scans, notifies saved meals, and advances the watermark', () async {
      final settings = await settingsWith(key: 'k');
      final intake = EmittingIntake()..batch = [photo(1), photo(2)];
      final analyzer = FakeAnalyzer()
        ..nextPhotoOutcome = const AnalysisOutcome(
            analysis: {'is_food': true, 'food_items': []},
            isFood: true,
            wall: Duration.zero);
      final cards = <String>[];
      final scanStart = DateTime(2026, 7, 23, 12, 0);
      final ok = await headlessBackfillWith(
        settings: settings,
        prefs: prefs,
        intake: intake,
        pipeline: () async => PhotoPipeline(dao: FakeDao(), analyzer: analyzer),
        showMealCard: (title, body) async => cards.add(body),
        clock: () => scanStart,
      );
      expect(ok, isTrue);
      expect(intake.scans, 1);
      expect(intake.sinceArgs.single, isNull); // first run: full window
      expect(cards, hasLength(2));
      expect(prefs.getString(backgroundWatermarkPrefsKey),
          scanStart.toIso8601String());
    });

    test('passes the persisted watermark minus the overlap', () async {
      final settings = await settingsWith(key: 'k');
      final mark = DateTime(2026, 7, 23, 11, 0);
      await prefs.setString(
          backgroundWatermarkPrefsKey, mark.toIso8601String());
      final intake = EmittingIntake();
      await headlessBackfillWith(
        settings: settings,
        prefs: prefs,
        intake: intake,
        pipeline: () async => PhotoPipeline(dao: FakeDao(), analyzer: FakeAnalyzer()),
        showMealCard: (_, _) async {},
      );
      expect(intake.sinceArgs.single, mark.subtract(backgroundScanOverlap));
    });

    test('does nothing when the watcher toggle is off', () async {
      final settings = await settingsWith(key: 'k', watcher: false);
      final intake = EmittingIntake();
      final ok = await headlessBackfillWith(
        settings: settings,
        prefs: prefs,
        intake: intake,
        pipeline: () async => PhotoPipeline(dao: FakeDao(), analyzer: FakeAnalyzer()),
        showMealCard: (_, _) async {},
      );
      expect(ok, isTrue); // success — never a WorkManager retry storm
      expect(intake.scans, 0);
      expect(prefs.getString(backgroundWatermarkPrefsKey), isNull);
    });

    test('does nothing without an API key (photos must not burn to failed)',
        () async {
      final settings = await settingsWith(key: null);
      final intake = EmittingIntake();
      final ok = await headlessBackfillWith(
        settings: settings,
        prefs: prefs,
        intake: intake,
        pipeline: () async => PhotoPipeline(dao: FakeDao(), analyzer: FakeAnalyzer()),
        showMealCard: (_, _) async {},
      );
      expect(ok, isTrue);
      expect(intake.scans, 0);
    });

    test('does nothing while the quota-pause latch is armed', () async {
      final settings = await settingsWith(
          key: 'k',
          quotaPauseUntil: DateTime.now().add(const Duration(hours: 3)));
      final intake = EmittingIntake();
      final ok = await headlessBackfillWith(
        settings: settings,
        prefs: prefs,
        intake: intake,
        pipeline: () async => PhotoPipeline(dao: FakeDao(), analyzer: FakeAnalyzer()),
        showMealCard: (_, _) async {},
      );
      expect(ok, isTrue);
      expect(intake.scans, 0);
      expect(prefs.getString(backgroundWatermarkPrefsKey), isNull);
    });

    test('retryable outcomes hold the final watermark at the frontier',
        () async {
      final settings = await settingsWith(key: 'k');
      final frontierMark = DateTime(2026, 7, 24, 3, 0);
      final intake = EmittingIntake()
        ..batch = [photo(1), photo(2)]
        ..frontier = frontierMark; // intake: coverage stops here
      final analyzer = FakeAnalyzer()
        ..nextPhotoOutcome = const AnalysisOutcome(
            error: 'rate limit', retryable: true, wall: Duration.zero);
      await headlessBackfillWith(
        settings: settings,
        prefs: prefs,
        intake: intake,
        pipeline: () async => PhotoPipeline(dao: FakeDao(), analyzer: analyzer),
        showMealCard: (_, _) async {},
        clock: () => DateTime(2026, 7, 24, 5, 0),
      );
      // NOT scanStart: released photos must stay ahead of the watermark.
      expect(prefs.getString(backgroundWatermarkPrefsKey),
          frontierMark.toIso8601String());
    });

    test('per-photo checkpoints persist the intake safeFrontier as they go',
        () async {
      final settings = await settingsWith(key: 'k');
      final intake = EmittingIntake()
        ..batch = [photo(1), photo(2)]
        ..safeFrontiers = [
          DateTime(2026, 7, 24, 1, 0),
          DateTime(2026, 7, 24, 2, 0),
        ];
      final seenMarks = <String?>[];
      final analyzer = FakeAnalyzer()
        ..nextPhotoOutcome = const AnalysisOutcome(
            analysis: {'is_food': true, 'food_items': []},
            isFood: true,
            wall: Duration.zero);
      await headlessBackfillWith(
        settings: settings,
        prefs: prefs,
        intake: intake,
        pipeline: () async => PhotoPipeline(
            dao: _CheckpointSpyDao(
                () => seenMarks.add(
                    prefs.getString(backgroundWatermarkPrefsKey))),
            analyzer: analyzer),
        showMealCard: (_, _) async {},
        clock: () => DateTime(2026, 7, 24, 5, 0),
      );
      // The SECOND photo's save observed the FIRST photo's checkpoint —
      // proof a WorkManager hard-stop mid-batch keeps prior progress.
      expect(seenMarks.length, 2);
      expect(seenMarks[1], DateTime(2026, 7, 24, 1, 0).toIso8601String());
    });

    test('an aborted scan does NOT advance the watermark', () async {
      final settings = await settingsWith(key: 'k');
      final intake = EmittingIntake()..throwAfterEmit = true;
      final ok = await headlessBackfillWith(
        settings: settings,
        prefs: prefs,
        intake: intake,
        pipeline: () async => PhotoPipeline(dao: FakeDao(), analyzer: FakeAnalyzer()),
        showMealCard: (_, _) async {},
      );
      expect(ok, isTrue); // still WorkManager success — no retry storm
      expect(prefs.getString(backgroundWatermarkPrefsKey), isNull);
    });

    test('failed analyses produce no meal card', () async {
      final settings = await settingsWith(key: 'k');
      final intake = EmittingIntake()..batch = [photo(1)];
      final cards = <String>[];
      await headlessBackfillWith(
        settings: settings,
        prefs: prefs,
        intake: intake,
        pipeline: () async => PhotoPipeline(dao: FakeDao(), analyzer: FakeAnalyzer()),
        showMealCard: (_, body) async => cards.add(body),
      );
      expect(cards, isEmpty);
    });
  });

  group('syncBackgroundScan', () {
    late RecordingScheduler scheduler;

    setUp(() {
      scheduler = RecordingScheduler();
      debugSchedulerOverride = scheduler;
      debugIsAndroidOverride = true;
    });

    tearDown(() {
      debugSchedulerOverride = null;
      debugIsAndroidOverride = null;
    });

    test('enabled → replace-registers the periodic job with settings values',
        () async {
      final settings = await settingsWith(key: 'k', lookback: 5);
      await syncBackgroundScan(settings);
      expect(scheduler.calls, [
        'init',
        'cancel:$photoBackfillUniqueName',
        'register:$photoBackfillUniqueName:$photoBackfillTaskName',
      ]);
      expect(scheduler.frequency, backgroundScanFrequency);
      expect(scheduler.inputData, {'lookbackDays': 5});
    });

    test('disabled → cancels and never registers', () async {
      final settings = await settingsWith(key: 'k', watcher: false);
      await syncBackgroundScan(settings);
      expect(scheduler.calls, ['cancel:$photoBackfillUniqueName']);
    });
  });

  group('HomeShell resume catch-up', () {
    UiServices services(FakeIntake intake, {required bool watcher}) =>
        UiServices(
          dao: FakeDao(),
          analyzer: FakeAnalyzer(),
          executor: FakeExecutor(),
          settings: FakeSettings(watcherEnabled: watcher),
          picker: FakePicker(),
          requestPhotoPermission: () async => true,
          photoIntake: intake,
          reports: FakeReports(),
        );

    testWidgets('resumed + watcher on → backfillScan', (tester) async {
      final intake = FakeIntake();
      await tester.pumpWidget(
          MaterialApp(home: HomeShell(services: services(intake, watcher: true))));
      final before = intake.backfillScans;
      tester.binding
          .handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump();
      expect(intake.backfillScans, before + 1);
      // Flush the delayed post-scan UI refresh so no timer is left pending.
      await tester.pump(const Duration(seconds: 16));
    });

    testWidgets('watcher off → no scan on resume', (tester) async {
      final intake = FakeIntake();
      await tester.pumpWidget(MaterialApp(
          home: HomeShell(services: services(intake, watcher: false))));
      final before = intake.backfillScans;
      tester.binding
          .handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump();
      expect(intake.backfillScans, before);
    });

    testWidgets('paused/inactive states never scan', (tester) async {
      final intake = FakeIntake();
      await tester.pumpWidget(
          MaterialApp(home: HomeShell(services: services(intake, watcher: true))));
      final before = intake.backfillScans;
      // Legal lifecycle order: inactive → hidden → paused.
      tester.binding
          .handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      await tester.pump();
      expect(intake.backfillScans, before);
    });
  });
}
