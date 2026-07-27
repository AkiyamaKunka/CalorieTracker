// Conformance tests against REAL SQLite (sqflite_common_ffi).
//
// Why this file exists: every other suite fakes MealsDao, so the 455 lines
// of actual SQL were essentially unverified. The design review proved it by
// mutation — it made recentMeals ignore its window, drop the is_food filter
// and order by `id DESC`; deleted deleteMeal's ledger tombstone; dropped
// `corrected: 1` from updateMealFields; and made isDuplicatePhoto always
// return false. All 597 tests still passed.
//
// The worst of those is recentMeals: it IS the snapshot the NL executor
// shows the model, so a broken window or order silently re-points
// "correct meal 2" at a different row in the owner's real food log. The
// Python side pins this against a real DB; this is the Dart mirror.
import 'dart:convert';
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/data/db.dart';
import 'package:calorie_tracker/data/meals_dao_impl.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  // Friday 2026-07-17, 20:00 local.
  final now = DateTime(2026, 7, 17, 20);
  late Database db;
  late SqfliteMealsDao dao;

  setUp(() async {
    db = await openAppDatabase(path: inMemoryDatabasePath);
    dao = SqfliteMealsDao(db, clock: () => now);
  });
  tearDown(() async => db.close());

  Future<int> insert({
    required String date,
    String time = '12:00 PM',
    String? timestamp,
    bool isFood = true,
    num cal = 100,
    String hash = '',
    String source = MealSource.appWatch,
  }) =>
      dao.saveMeal(Meal(
        id: 0,
        date: date,
        time: time,
        timestamp: timestamp ?? '${date}T12:00:00.000',
        source: source,
        imageHash: hash,
        analysis: {'is_food': isFood, 'total_calories': cal},
      ));

  group('recentMeals — the NL snapshot (window, filter, ORDER)', () {
    test('includes the edge day, excludes the day before it', () async {
      // days: 7 → window is [today-6 .. today] inclusive.
      await insert(date: '2026-07-11', cal: 111); // edge day: IN
      await insert(date: '2026-07-10', cal: 222); // one day earlier: OUT
      await insert(date: '2026-07-17', cal: 333);
      final rows = await dao.recentMeals();
      final cals =
          rows.map((m) => m.analysis['total_calories']).toList();
      expect(cals, [111, 333]);
    });

    test('excludes non-food rows (Python truthiness, not == true)', () async {
      await insert(date: '2026-07-17', isFood: true, cal: 1);
      await insert(date: '2026-07-17', isFood: false, cal: 2);
      final rows = await dao.recentMeals();
      expect(rows.map((m) => m.analysis['total_calories']), [1]);
    });

    test('orders by timestamp ASC, not by insertion id', () async {
      // Insert newest FIRST so an `id ASC/DESC` order would disagree.
      await insert(
          date: '2026-07-17',
          cal: 999,
          timestamp: '2026-07-17T19:00:00.000');
      await insert(
          date: '2026-07-17',
          cal: 111,
          timestamp: '2026-07-17T07:00:00.000');
      final rows = await dao.recentMeals();
      expect(rows.map((m) => m.analysis['total_calories']), [111, 999],
          reason: 'the model numbers meals in this order — a wrong order '
              'makes "correct meal 2" hit the wrong row');
    });

    test('honors a custom day count', () async {
      await insert(date: '2026-07-16', cal: 1);
      await insert(date: '2026-07-17', cal: 2);
      expect((await dao.recentMeals(days: 1)).length, 1);
    });
  });

  group('mealsBetween', () {
    test('bounds are inclusive on both ends and ordered by timestamp',
        () async {
      await insert(date: '2026-07-15', cal: 1);
      await insert(date: '2026-07-16', cal: 2);
      await insert(date: '2026-07-17', cal: 3);
      final rows = await dao.mealsBetween('2026-07-15', '2026-07-16');
      expect(rows.map((m) => m.analysis['total_calories']), [1, 2]);
    });

    test('returns non-food rows too (views filter, the DAO does not)',
        () async {
      await insert(date: '2026-07-17', isFood: false);
      expect(await dao.mealsBetween('2026-07-17', '2026-07-17'), hasLength(1));
    });
  });

  group('updateMealFields', () {
    test('analysis-only edit sets corrected and leaves date/time/timestamp',
        () async {
      final id = await insert(date: '2026-07-17', time: '01:00 PM');
      await dao.updateMealFields(id, analysis: {'is_food': true, 'x': 1});
      final m = (await dao.mealsBetween('2026-07-17', '2026-07-17')).single;
      expect(m.analysis['x'], 1);
      expect(m.corrected, isTrue);
      expect(m.date, '2026-07-17');
      expect(m.time, '01:00 PM');
      expect(m.timestamp, '2026-07-17T12:00:00.000'); // ingestion time kept
    });

    test('date-only and time-only edits each move just that field', () async {
      final id = await insert(date: '2026-07-17', time: '01:00 PM');
      await dao.updateMealFields(id, date: '2026-07-16');
      var m = (await dao.mealsBetween('2026-07-16', '2026-07-16')).single;
      expect(m.time, '01:00 PM');
      await dao.updateMealFields(id, time: '08:30 AM');
      m = (await dao.mealsBetween('2026-07-16', '2026-07-16')).single;
      expect(m.date, '2026-07-16');
      expect(m.time, '08:30 AM');
    });

    test('an unknown id is a no-op, not an error', () async {
      await dao.updateMealFields(9999, analysis: const {'is_food': true});
      expect(await dao.mealsBetween('2000-01-01', '2100-01-01'), isEmpty);
    });
  });

  group('deleteMeal — the anti-resurrection rule', () {
    test('removes the row, TOMBSTONES the ledger, and drops the thumb',
        () async {
      const hash = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6';
      final id = await dao.saveMeal(
        Meal(
          id: 0,
          date: '2026-07-17',
          time: '12:00 PM',
          timestamp: '2026-07-17T12:00:00.000',
          source: MealSource.appWatch,
          imageHash: hash,
          analysis: const {'is_food': true},
        ),
        markStatus: IngestionStatus.saved,
      );
      await dao.saveMealThumb(id, Uint8List.fromList([1, 2, 3]));

      await dao.deleteMeal(id);

      expect(await dao.mealsBetween('2026-07-17', '2026-07-17'), isEmpty);
      final row = await dao.photoStatus(hash);
      expect(row!.status, IngestionStatus.deleted,
          reason: 'without the tombstone the §6 backfill re-logs the photo');
      expect(row.mealId, isNull);
      expect(await dao.mealThumb(id), isNull);
      // And the strict automated path must refuse it now.
      expect(
          await dao.reservePhotoHash(hash, source: MealSource.appWatch),
          isFalse);
    });
  });

  group('isDuplicatePhoto — the 5-minute same-photo window', () {
    Future<void> seedToday(String timestamp) => dao.saveMeal(Meal(
          id: 0,
          date: '2026-07-17',
          time: '08:00 PM',
          timestamp: timestamp,
          source: MealSource.appPhoto,
          imageHash: 'f' * 32,
          analysis: const {'is_food': true},
        ));

    test('4 minutes ago is a duplicate, 6 minutes ago is not', () async {
      await seedToday('2026-07-17T19:56:00.000'); // 4 min before `now`
      expect(await dao.isDuplicatePhoto('f' * 32), isTrue);
    });

    test('outside the window it is not a duplicate', () async {
      await seedToday('2026-07-17T19:54:00.000'); // 6 min before `now`
      expect(await dao.isDuplicatePhoto('f' * 32), isFalse);
    });

    test('a match on ANOTHER date does not count', () async {
      await dao.saveMeal(Meal(
        id: 0,
        date: '2026-07-16',
        time: '08:00 PM',
        timestamp: '2026-07-17T19:56:00.000',
        source: MealSource.appPhoto,
        imageHash: 'f' * 32,
        analysis: const {'is_food': true},
      ));
      expect(await dao.isDuplicatePhoto('f' * 32), isFalse);
    });

    test('a blank hash is never a duplicate', () async {
      expect(await dao.isDuplicatePhoto('   '), isFalse);
    });
  });

  group('fitness writes (never executed by any prior test)', () {
    test('saveBodyWeight upserts: one row per day, last value wins', () async {
      await dao.saveBodyWeight('2026-07-17', 72.5);
      await dao.saveBodyWeight('2026-07-17', 71.8);
      final rows = await db.query('body_weight');
      expect(rows, hasLength(1));
      expect(rows.single['weight_kg'], 71.8);
      expect(rows.single['source'], 'nl');
    });

    test('saveActivity stores 0 as NULL and steps inside raw JSON', () async {
      await dao.saveActivity('2026-07-17',
          activeCalories: 0, steps: 9000, distanceKm: 0);
      final row = (await db.query('activities')).single;
      expect(row['active_calories'], isNull);
      expect(row['distance_km'], isNull);
      expect(jsonDecode(row['raw']! as String), {'steps': 9000});
      expect(row['external_id'], isNull, reason: 'manual rows always insert');
    });

    test('a zero step count leaves raw NULL', () async {
      await dao.saveActivity('2026-07-17', activeCalories: 300, steps: 0);
      final row = (await db.query('activities')).single;
      expect(row['raw'], isNull);
      expect(row['active_calories'], 300);
    });
  });

  test('exportJson wraps every table in the versioned envelope', () async {
    await insert(date: '2026-07-17');
    await dao.saveBodyWeight('2026-07-17', 70);
    final decoded = jsonDecode(await dao.exportJson()) as Map<String, dynamic>;
    expect(decoded['format'], 'calorie_tracker_export');
    expect(decoded['version'], 1);
    final tables = decoded['tables'] as Map<String, dynamic>;
    for (final t in kExportTables) {
      expect(tables.containsKey(t), isTrue, reason: t);
    }
    expect(tables['meals'], hasLength(1));
    expect(tables['body_weight'], hasLength(1));
  });
}
