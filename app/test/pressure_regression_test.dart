// Regression pins for the 2026-08-03 pressure-test campaign (wave 1).
// Every test here reproduces a bug an adversarial fuzzer FOUND and an
// independent verifier CONFIRMED against the pre-fix code — if one fails,
// a fixed bug is back.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:calorie_tracker/core/coerce.dart';
import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/data/db.dart';
import 'package:calorie_tracker/data/meals_dao_impl.dart';
import 'package:calorie_tracker/services/garmin_client.dart';
import 'package:calorie_tracker/services/nl/executor.dart';
import 'package:calorie_tracker/ui/format.dart';
import 'package:calorie_tracker/ui/photo_pipeline.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

import 'nl/fakes.dart';
import 'ui/fakes.dart' as uifakes show FakeAnalyzer;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  group('non-finite numbers can no longer crash or drop data', () {
    test('formatKcal degrades NaN/Infinity to 0 instead of throwing', () {
      expect(formatKcal(double.infinity), '0');
      expect(formatKcal(double.negativeInfinity), '0');
      expect(formatKcal(double.nan), '0');
      expect(formatKcal(1234), '1,234'); // the normal path is untouched
    });

    test('a hostile Garmin body cannot put Infinity on the Today screen',
        () {
      final g = parseGarminDailyBody(
          200, '{"available":true,"active_calories":1e400,"steps":1e400}');
      expect(g, isNotNull);
      expect(g!.activeCalories.isFinite, isTrue);
      expect(g.activeCalories, 0);
    });

    test('finiteAnalysis makes any model reply encodable, 0-for-nonfinite',
        () {
      final out = finiteAnalysis({
        'total_calories': double.infinity,
        'note': 'keep',
        'food_items': [
          {'name': 'cake', 'estimated_calories': double.nan},
        ],
        'nested': {'deep': double.negativeInfinity},
      });
      expect(jsonEncode(out), isNotEmpty); // must not throw
      expect(out['total_calories'], 0);
      expect(out['note'], 'keep');
      expect((out['food_items'] as List).first['estimated_calories'], 0);
      expect((out['nested'] as Map)['deep'], 0);
    });
  });

  group('real-DB regressions', () {
    late Directory tmp;
    late Database db;
    late SqfliteMealsDao dao;
    final now = DateTime(2026, 8, 3, 12);

    setUp(() async {
      tmp = await Directory.systemTemp.createTemp('ct_pressure');
      db = await openAppDatabase(path: '${tmp.path}/a.db');
      dao = SqfliteMealsDao(db, clock: () => now);
    });
    tearDown(() async {
      await db.close();
      await tmp.delete(recursive: true);
    });

    test('a 1e400 model number SAVES the meal (0-coerced) instead of '
        'silently dropping it', () async {
      final analysis = jsonDecode('{"is_food":true,'
          '"meal_description":"huge cake","total_calories":1e400,'
          '"food_items":[{"name":"cake","estimated_calories":1e400}]}');
      final exec =
          DefaultNlExecutor(dao, FakeAnalyzer(), await testSettings());
      final replies = await exec.executeParsed(
          {'intent': 'new_meal', 'analysis': analysis},
          'i ate a huge cake',
          const []);
      expect(replies.map((r) => r.text).join('\n'), contains('✅'),
          reason: 'the meal must save, not "please try again"');
      // TODAY computed, not pinned — the executor dates meals with the
      // real clock (the pinned form detonated at the first midnight).
      final today = isoDate(DateTime.now());
      final saved = await dao.mealsBetween(today, today);
      expect(saved, hasLength(1));
      expect(saved.single.analysis['total_calories'], 0,
          reason: 'safeNumber fallback, matching read-side coercion');
    });

    test('editing a photoless meal no longer makes an older backup '
        'duplicate it on import', () async {
      final id = await dao.saveMeal(Meal(
        id: 0,
        date: '2026-07-31',
        time: '08:00 AM',
        timestamp: '2026-07-31T08:00:00',
        source: MealSource.manualText,
        imageHash: '',
        fileId: '',
        analysis: {'is_food': true, 'total_calories': 500},
      ));
      final backup = await dao.exportJson();
      await dao.updateMealFields(id,
          analysis: {'is_food': true, 'total_calories': 650});
      await dao.importJson(backup);
      final rows = await dao.mealsBetween('2026-07-31', '2026-07-31');
      expect(rows, hasLength(1),
          reason: 'the edited row IS the exported meal — no duplicate');
      expect(rows.single.analysis['total_calories'], 650,
          reason: 'the edit wins; the stale backup copy must not land');
    });

    test('two DIFFERENT same-second text meals still both import '
        '(the dedup fix must not collapse them)', () async {
      for (final (cal, desc) in [(100, 'coffee'), (350, 'sandwich')]) {
        await dao.saveMeal(Meal(
          id: 0,
          date: '2026-07-31',
          time: '08:00 AM',
          timestamp: '2026-07-31T08:00:00', // second-resolution collision
          source: MealSource.manualText,
          imageHash: '',
          fileId: '',
          analysis: {
            'is_food': true,
            'total_calories': cal,
            'meal_description': desc
          },
        ));
      }
      final backup = await dao.exportJson();
      final tmp2 = await Directory.systemTemp.createTemp('ct_pressure_b');
      final db2 = await openAppDatabase(path: '${tmp2.path}/b.db');
      try {
        final dao2 = SqfliteMealsDao(db2, clock: () => now);
        await dao2.importJson(backup);
        expect(await dao2.mealsBetween('2026-07-31', '2026-07-31'),
            hasLength(2));
        // And a repeat import is still a no-op.
        await dao2.importJson(backup);
        expect(await dao2.mealsBetween('2026-07-31', '2026-07-31'),
            hasLength(2));
      } finally {
        await db2.close();
        await tmp2.delete(recursive: true);
      }
    });
  });

  group('pipeline containment', () {
    test('a throwing post-save callback cannot un-report a committed meal',
        () async {
      final tmp = await Directory.systemTemp.createTemp('ct_pipe');
      final db = await openAppDatabase(path: '${tmp.path}/p.db');
      try {
        final dao = SqfliteMealsDao(db, clock: () => DateTime(2026, 8, 3));
        final analyzer = uifakes.FakeAnalyzer()
          ..nextPhotoOutcome = const AnalysisOutcome(
              analysis: {'is_food': true, 'total_calories': 300},
              isFood: true,
              wall: Duration.zero);
        const hash = 'cafe0001cafe0001cafe0001cafe0001';
        final pipeline = PhotoPipeline(
            dao: dao,
            analyzer: analyzer,
            hasher: (_) async => hash,
            notify: (_) => throw StateError('snackbar died'));
        final outcome = await pipeline.process(IntakePhoto(
            Uint8List.fromList([1, 2, 3]), 'a1', 'IMG_1.jpg',
            deliberate: true));
        expect(outcome.kind, PhotoOutcomeKind.saved,
            reason: 'the meal committed — the report must say so');
        final status = await dao.photoStatus(hash);
        expect(status?.status, IngestionStatus.saved,
            reason: 'the ledger row must not flip saved→failed');
      } finally {
        await db.close();
        await tmp.delete(recursive: true);
      }
    });
  });

  group('parser hardening', () {
    test("'overweight' no longer arms the weigh-keyword parser", () {
      expect(parseWeightKg('overweight, ate 200 calories today'), isNull);
      expect(parseWeightKg('I am underweight 200'), isNull);
      expect(parseWeightKg('I weigh 72 kg'), 72.0); // real weigh-ins intact
      expect(parseWeightKg('weighed 81.6'), 81.6);
    });

    test('fullwidth Chinese-IME digits parse; exotic digit sets reject '
        '(both matching the server)', () {
      expect(parseWeightKg('７２．５kg'), 72.5);
      expect(parseWeightKg('weigh ８１'), 81.0);
      expect(parseWeightKg('٧٢kg'), isNull);
    });

    test('parseBoolish strips C0 separators like Python str.strip()', () {
      for (final sep in ['\x1c', '\x1d', '\x1e', '\x1f']) {
        expect(parseBoolish('${sep}yes$sep'), isTrue, reason: 'sep $sep');
        expect(parseBoolish('${sep}false$sep'), isFalse);
      }
      expect(parseBoolish('maybe'), isNull); // reject path untouched
    });

    test('friendlyHistoryDay degrades impossible dates to the raw string',
        () {
      final now = DateTime(2026, 8, 3);
      expect(friendlyHistoryDay('2026-02-29', now: now), '2026-02-29');
      expect(friendlyHistoryDay('2026-13-45', now: now), '2026-13-45');
      expect(friendlyHistoryDay('2026-02-30', now: now), '2026-02-30');
      expect(friendlyHistoryDay('2026-08-01', now: now), 'Saturday, Aug 01');
    });
  });
}
