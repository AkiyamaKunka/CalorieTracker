// Data IMPORT against real SQLite (the export's missing half, 2026-07-31).
//
// Why it must be conformance-tested, not faked: import is the only code
// that WRITES rows the app did not create, and its dedup rules are pure
// SQL. A wrong rule either doubles the owner's calorie history or silently
// drops meals — both invisible until the totals look wrong weeks later.
//
// It also unblocks two real events: moving to a new phone, and the
// eventual release-keystore re-sign (Android treats a re-signed APK as a
// different app and wipes its data).
import 'dart:convert';
import 'dart:io';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/data/db.dart';
import 'package:calorie_tracker/data/meals_dao_impl.dart';
import 'package:calorie_tracker/data/meals_logic.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  final now = DateTime(2026, 7, 31, 16);
  late Database dbA; // the "old phone"
  late Database dbB; // the "new phone"
  late SqfliteMealsDao daoA;
  late SqfliteMealsDao daoB;

  late Directory tmp;

  setUp(() async {
    // DISTINCT files, not two ':memory:' handles — sqflite returns the
    // SAME in-memory database for that path, so "old phone" and "new
    // phone" would silently be one device and every import would look
    // like a duplicate.
    tmp = await Directory.systemTemp.createTemp('ct_import');
    dbA = await openAppDatabase(path: '${tmp.path}/a.db');
    dbB = await openAppDatabase(path: '${tmp.path}/b.db');
    daoA = SqfliteMealsDao(dbA, clock: () => now);
    daoB = SqfliteMealsDao(dbB, clock: () => now);
  });
  tearDown(() async {
    await dbA.close();
    await dbB.close();
    await tmp.delete(recursive: true);
  });

  Future<int> seed(SqfliteMealsDao dao,
          {required String date,
          String time = '12:00 PM',
          num cal = 500,
          String hash = ''}) =>
      dao.saveMeal(
          Meal(
            id: 0,
            date: date,
            time: time,
            timestamp: '${date}T12:00:00.000',
            source: MealSource.appWatch,
            imageHash: hash,
            analysis: {'is_food': true, 'total_calories': cal},
          ),
          markStatus: hash.isEmpty ? null : IngestionStatus.saved);

  test('a full export moves the food log to a fresh phone', () async {
    await seed(daoA, date: '2026-07-29', cal: 610, hash: 'h1');
    await seed(daoA, date: '2026-07-30', cal: 720, hash: 'h2');
    await daoA.saveBodyWeight('2026-07-30', 72.4);
    await daoA.saveActivity('2026-07-30', steps: 8000, activeCalories: 300);

    final summary = await daoB.importJson(await daoA.exportJson());

    expect(summary.added['meals'], 2);
    expect(summary.added['body_weight'], 1);
    expect(summary.added['activities'], 1);
    expect(summary.exportedAt, isNotNull);
    final moved = await daoB.mealsBetween('2026-07-01', '2026-07-31');
    expect(moved.map((m) => m.analysis['total_calories']), [610, 720]);
    // The ledger travels too, so the new phone's watcher does not
    // re-analyze photos the old phone already logged.
    expect((await daoB.photoStatus('h1'))?.status, IngestionStatus.saved);
  });

  test('importing the SAME file twice adds nothing the second time',
      () async {
    await seed(daoA, date: '2026-07-29', cal: 610, hash: 'h1');
    final payload = await daoA.exportJson();
    await daoB.importJson(payload);
    final second = await daoB.importJson(payload);

    expect(second.totalAdded, 0, reason: 're-import must never double the '
        "owner's calories");
    expect(second.skipped['meals'], 1);
    expect(await daoB.mealsBetween('2026-07-01', '2026-07-31'), hasLength(1));
  });

  test('merge keeps rows that exist only on the target', () async {
    await seed(daoA, date: '2026-07-29', cal: 610, hash: 'h1');
    await seed(daoB, date: '2026-07-31', cal: 200, hash: 'local');

    await daoB.importJson(await daoA.exportJson());

    final all = await daoB.mealsBetween('2026-07-01', '2026-07-31');
    expect(all.map((m) => m.analysis['total_calories']), [610, 200],
        reason: 'import is a MERGE — local meals must survive');
  });

  test('two DIFFERENT meals at the same clock time both survive', () async {
    // The dedup key includes the analysis: a coffee and a sandwich logged
    // in the same minute are two meals, not a duplicate.
    await seed(daoA, date: '2026-07-30', time: '08:00 AM', cal: 90);
    await seed(daoA, date: '2026-07-30', time: '08:00 AM', cal: 450);
    final summary = await daoB.importJson(await daoA.exportJson());
    expect(summary.added['meals'], 2);
  });

  test('imported ids never collide with local ones', () async {
    await seed(daoB, date: '2026-07-31', cal: 111, hash: 'local');
    await seed(daoA, date: '2026-07-29', cal: 610, hash: 'h1');
    await daoB.importJson(await daoA.exportJson());

    final all = await daoB.mealsBetween('2026-07-01', '2026-07-31');
    final ids = all.map((m) => m.id).toList();
    expect(ids.toSet(), hasLength(ids.length),
        reason: 'a reused primary key would overwrite a real meal');
    // And both rows are independently editable afterwards.
    await daoB.updateMealFields(ids.first, date: '2026-07-28');
    final after = await daoB.mealsBetween('2026-07-01', '2026-07-31');
    expect(after, hasLength(2));
  });

  test('a photo-ingestion row never points at a foreign meal id', () async {
    await seed(daoA, date: '2026-07-29', cal: 610, hash: 'h1');
    await daoB.importJson(await daoA.exportJson());
    final rows = await dbB.query('photo_ingestions');
    expect(rows.single['meal_id'], isNull,
        reason: 'the old id means nothing here; status is what matters');
    expect(rows.single['status'], 'saved');
  });

  test('an EDITED meal does not re-import as a duplicate of its backup',
      () async {
    // The identity used to include the analysis JSON, so any correction
    // (every edit path rewrites analysis) made an older backup look like a
    // different meal — importing it would have shown the same lunch twice,
    // once with the wrong numbers. A PHOTO meal's identity is its photo.
    final id = await seed(daoA, date: '2026-07-29', cal: 610, hash: 'h1');
    final backup = await daoA.exportJson(); // taken BEFORE the correction
    await daoA.updateMealFields(id,
        analysis: {'is_food': true, 'total_calories': 480});
    await daoB.importJson(await daoA.exportJson()); // current state
    final summary = await daoB.importJson(backup); // the older file

    expect(summary.added['meals'], 0,
        reason: 'the corrected meal and its pre-edit ancestor are ONE meal');
    final meals = await daoB.mealsBetween('2026-07-01', '2026-07-31');
    expect(meals, hasLength(1));
    expect(meals.single.analysis['total_calories'], 480,
        reason: 'the corrected numbers must survive');
  });

  test('two photoless meals in the same minute stay distinct', () async {
    // Without a photo there is no stable anchor, so date+time+analysis is
    // the fallback identity — and a coffee is not a sandwich.
    await daoA.saveMeal(Meal(
        id: 0,
        date: '2026-07-30',
        time: '08:00 AM',
        timestamp: '2026-07-30T08:00:00.000',
        source: MealSource.manualText,
        analysis: {'is_food': true, 'total_calories': 90}));
    await daoA.saveMeal(Meal(
        id: 0,
        date: '2026-07-30',
        time: '08:00 AM',
        timestamp: '2026-07-30T08:00:00.000',
        source: MealSource.manualText,
        analysis: {'is_food': true, 'total_calories': 450}));
    final summary = await daoB.importJson(await daoA.exportJson());
    expect(summary.added['meals'], 2);
  });

  test('two activities on the SAME DAY both survive an import', () async {
    // The dedup key was date-only at first, which would have collapsed a
    // morning walk and an evening run into one row (review 2026-07-31).
    await daoA.saveActivity('2026-07-30', steps: 3000, activeCalories: 120);
    await daoA.saveActivity('2026-07-30', steps: 9000, activeCalories: 480);
    final summary = await daoB.importJson(await daoA.exportJson());
    expect(summary.added['activities'], 2);
    final rows = await dbB.query('activities');
    expect(rows, hasLength(2));
  });

  test('a row rejected by SQLite counts as skipped, never as added',
      () async {
    // INSERT OR IGNORE swallows constraint violations and returns 0
    // instead of throwing, so an unconditional counter reported rows that
    // never landed — and this summary is the user's only evidence the
    // transfer worked.
    final payload = jsonEncode({
      'format': kExportFormat,
      'version': 1,
      'tables': {
        'meals': [
          // NOT NULL violations: no date/time/timestamp/analysis.
          {'chat_id': 1},
          {
            'chat_id': 1,
            'date': '2026-07-30',
            'time': '01:00 PM',
            'timestamp': '2026-07-30T13:00:00.000',
            'source': 'app_photo',
            'image_hash': 'good',
            'analysis': '{"is_food":true,"total_calories":300}',
          },
        ]
      },
    });
    final summary = await daoB.importJson(payload);
    expect(summary.added['meals'], 1);
    expect(summary.skipped['meals'], 1);
    expect(await daoB.mealsBetween('2026-07-01', '2026-07-31'), hasLength(1));
  });

  group('refusal', () {
    test('non-JSON, wrong format tag, and missing tables all throw',
        () async {
      await expectLater(
          daoB.importJson('hello'), throwsA(isA<FormatException>()));
      await expectLater(
          daoB.importJson(jsonEncode({'format': 'someone_elses_app'})),
          throwsA(isA<FormatException>()));
      await expectLater(
          daoB.importJson(jsonEncode(
              {'format': kExportFormat, 'version': 1, 'tables': 'nope'})),
          throwsA(isA<FormatException>()));
      expect(await daoB.mealsBetween('2026-01-01', '2026-12-31'), isEmpty,
          reason: 'a refused import writes NOTHING');
    });

    test('a NEWER export version still imports (forward tolerance)',
        () async {
      await seed(daoA, date: '2026-07-29', cal: 610, hash: 'h1');
      final envelope =
          jsonDecode(await daoA.exportJson()) as Map<String, dynamic>;
      envelope['version'] = 99;
      (envelope['tables'] as Map)['some_future_table'] = [
        {'whatever': 1}
      ];
      final summary = await daoB.importJson(jsonEncode(envelope));
      expect(summary.added['meals'], 1,
          reason: 'a user mid-upgrade must not be stranded');
    });

    test('a row with junk columns is skipped, not fatal', () async {
      final payload = jsonEncode({
        'format': kExportFormat,
        'version': 1,
        'exported_at': '2026-07-31T10:00:00.000',
        'tables': {
          'meals': [
            {'not_a_column': 'x'},
            {
              'chat_id': 0,
              'date': '2026-07-30',
              'time': '01:00 PM',
              'timestamp': '2026-07-30T13:00:00.000',
              'source': 'app_photo',
              'image_hash': 'ok',
              'analysis': '{"is_food":true,"total_calories":300}',
            },
          ]
        },
      });
      final summary = await daoB.importJson(payload);
      expect(summary.added['meals'], 1, reason: 'the good row lands');
      expect(summary.skipped['meals'], 1, reason: 'the junk row does not');
    });
  });
}
