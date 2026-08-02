// Body page data layer against real SQLite (2026-08-02).
//
// body_weight is the parity table the NL executor has been writing all
// along; body_measurements is new and app-only (spec §9). Real SQL, not
// fakes: upsert/merge semantics and the v2→v3 migration are exactly the
// kind of behavior a fake silently gets wrong.
import 'dart:convert';
import 'dart:io';

import 'package:calorie_tracker/data/db.dart';
import 'package:calorie_tracker/data/meals_dao_impl.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  sqfliteFfiInit();
  databaseFactory = databaseFactoryFfi;

  final now = DateTime(2026, 8, 2, 10);
  late Database db;
  late SqfliteMealsDao dao;
  late Directory tmp;

  setUp(() async {
    tmp = await Directory.systemTemp.createTemp('ct_body');
    db = await openAppDatabase(path: '${tmp.path}/t.db');
    dao = SqfliteMealsDao(db, clock: () => now);
  });
  tearDown(() async {
    await db.close();
    await tmp.delete(recursive: true);
  });

  group('body_weight', () {
    test('same-day save is an upsert, and list is ascending', () async {
      await dao.saveBodyWeight('2026-08-01', 82.0, source: 'manual');
      await dao.saveBodyWeight('2026-08-02', 81.9, source: 'manual');
      await dao.saveBodyWeight('2026-08-02', 81.6, source: 'manual');

      final all = await dao.listBodyWeights('2026-01-01', '2026-12-31');
      expect([for (final w in all) (w.date, w.kg)],
          [('2026-08-01', 82.0), ('2026-08-02', 81.6)],
          reason: 'one canonical weigh-in per day — the re-log wins');
    });

    test('the window is inclusive on both ends', () async {
      await dao.saveBodyWeight('2026-08-01', 82.0);
      await dao.saveBodyWeight('2026-08-02', 81.6);
      await dao.saveBodyWeight('2026-08-03', 81.5);
      final window = await dao.listBodyWeights('2026-08-01', '2026-08-02');
      expect([for (final w in window) w.date], ['2026-08-01', '2026-08-02']);
    });

    test('UI writes carry source manual; executor default stays nl',
        () async {
      await dao.saveBodyWeight('2026-08-01', 82.0); // executor call shape
      await dao.saveBodyWeight('2026-08-02', 81.6, source: 'manual');
      final all = await dao.listBodyWeights('2026-01-01', '2026-12-31');
      expect(all.first.source, 'nl');
      expect(all.last.source, 'manual');
    });

    test('delete removes exactly the one day', () async {
      await dao.saveBodyWeight('2026-08-01', 82.0);
      await dao.saveBodyWeight('2026-08-02', 81.6);
      await dao.deleteBodyWeight('2026-08-01');
      final all = await dao.listBodyWeights('2026-01-01', '2026-12-31');
      expect([for (final w in all) w.date], ['2026-08-02']);
    });
  });

  group('body_measurements', () {
    test('full-row upsert: a re-save overwrites the whole day', () async {
      await dao.saveBodyMeasurements('2026-08-02',
          waistCm: 84, chestCm: 100, hipCm: 98);
      // The sheet is prefilled, so a save with only waist means the user
      // CLEARED chest and hip — null must overwrite, not merge.
      await dao.saveBodyMeasurements('2026-08-02', waistCm: 83.5);

      final all = await dao.listBodyMeasurements('2026-01-01', '2026-12-31');
      expect(all, hasLength(1));
      expect(all.single.waistCm, 83.5);
      expect(all.single.chestCm, isNull);
      expect(all.single.hipCm, isNull);
    });

    test('an all-empty save deletes the row instead of storing a blank',
        () async {
      await dao.saveBodyMeasurements('2026-08-02', waistCm: 84);
      await dao.saveBodyMeasurements('2026-08-02');
      expect(await dao.listBodyMeasurements('2026-01-01', '2026-12-31'),
          isEmpty);
    });

    test('list is ascending and delete is per-day', () async {
      await dao.saveBodyMeasurements('2026-08-02', chestCm: 100);
      await dao.saveBodyMeasurements('2026-08-01', waistCm: 84);
      await dao.deleteBodyMeasurements('2026-08-02');
      final all = await dao.listBodyMeasurements('2026-01-01', '2026-12-31');
      expect([for (final m in all) m.date], ['2026-08-01']);
      expect(all.single.waistCm, 84);
    });
  });

  test('v2 database gains the body_measurements table on reopen (migration)',
      () async {
    // Simulate an existing install: a database created WITHOUT the new
    // table at version 2, then opened by the new code at version 3.
    final path = '${tmp.path}/old.db';
    final old = await databaseFactory.openDatabase(path,
        options: OpenDatabaseOptions(
          version: 2,
          onCreate: (db, v) async {
            for (final ddl in kSchemaDdl) {
              if (ddl.contains('body_measurements')) continue;
              await db.execute(ddl);
            }
          },
        ));
    await old.close();

    final upgraded = await openAppDatabase(path: path);
    final tables = await upgraded.rawQuery(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='body_measurements'");
    expect(tables, hasLength(1),
        reason: 'onUpgrade re-runs the IF NOT EXISTS DDL list');
    final dao2 = SqfliteMealsDao(upgraded, clock: () => now);
    await dao2.saveBodyMeasurements('2026-08-02', waistCm: 84);
    expect(await dao2.listBodyMeasurements('2026-01-01', '2026-12-31'),
        hasLength(1));
    await upgraded.close();
  });

  test('export/import round-trips measurements and dedups on re-import',
      () async {
    await dao.saveBodyWeight('2026-08-02', 81.6, source: 'manual');
    await dao.saveBodyMeasurements('2026-08-02', waistCm: 84, hipCm: 98);
    final envelope = await dao.exportJson();
    expect(jsonDecode(envelope)['tables'], contains('body_measurements'));

    final db2 = await openAppDatabase(path: '${tmp.path}/other.db');
    final dao2 = SqfliteMealsDao(db2, clock: () => now);
    final first = await dao2.importJson(envelope);
    expect(first.added['body_measurements'], 1);
    expect(first.added['body_weight'], 1);

    final again = await dao2.importJson(envelope);
    expect(again.added['body_measurements'], 0,
        reason: 'same chat_id+date must be recognized, not doubled');
    expect(again.skipped['body_measurements'], 1);

    final rows = await dao2.listBodyMeasurements('2026-01-01', '2026-12-31');
    expect(rows.single.waistCm, 84);
    expect(rows.single.chestCm, isNull);
    expect(rows.single.hipCm, 98);
    await db2.close();
  });
}
