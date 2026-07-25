// Real-SQLite tests for the reclaim sweep (spec §2.3 as adjusted for the
// two-isolate reality) — the first DB-backed harness in the suite: the
// reclaim SQL had ZERO tests before this file (loop cycle 2 critical gap).
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

  final now = DateTime(2026, 7, 24, 4, 0, 0);

  late Database db;
  late SqfliteMealsDao dao;

  setUp(() async {
    db = await openAppDatabase(path: inMemoryDatabasePath);
    dao = SqfliteMealsDao(db, clock: () => now);
  });

  tearDown(() async => db.close());

  Future<void> seedProcessing(String hash, String source,
      {required Duration age}) async {
    await dao.reservePhotoHash(hash, source: source);
    // Backdate the row: reservePhotoHash stamps last_seen_at = now.
    await db.update(
      'photo_ingestions',
      {'last_seen_at': now.subtract(age).toIso8601String()},
      where: 'image_hash = ?',
      whereArgs: [hash],
    );
  }

  test('STALE app_watch processing rows are RELEASED (deleted), not burned',
      () async {
    await seedProcessing('a' * 32, 'app_watch',
        age: const Duration(minutes: 30));
    await dao.reclaimStaleProcessing();
    // Released row = re-reservable by the strict automated path.
    final again = await dao.reservePhotoHash('a' * 32, source: 'app_watch');
    expect(again, isTrue,
        reason: 'released watch rows must be re-offerable — burning them '
            'permanently dropped interrupted photos (live bug 2026-07-23)');
  });

  test('STALE deliberate processing rows are marked failed (retry surface)',
      () async {
    await seedProcessing('b' * 32, 'app_photo',
        age: const Duration(minutes: 30));
    await dao.reclaimStaleProcessing();
    // failed → strict path refuses, deliberate re-add reclaims (spec §2.3).
    expect(
        await dao.reservePhotoHash('b' * 32, source: 'app_watch'), isFalse);
    expect(
        await dao.reservePhotoHash('b' * 32,
            source: 'app_photo', reclaimDeliberate: true),
        isTrue);
  });

  test('FRESH processing rows are untouched — the other isolate may own them',
      () async {
    await seedProcessing('c' * 32, 'app_watch',
        age: const Duration(minutes: 5)); // < 15-min staleness threshold
    await dao.reclaimStaleProcessing();
    // Still reserved: neither released nor failed.
    expect(await dao.reservePhotoHash('c' * 32, source: 'app_watch'), isFalse,
        reason: 'a live background run\'s reservation must survive the '
            'launch sweep (double-log race otherwise)');
  });

  test('meal_thumbs: round-trip, replace, delete cascade (spec §9)', () async {
    final id = await dao.saveMeal(Meal(
      id: 0,
      date: '2026-07-26',
      time: '12:00 PM',
      timestamp: now.toIso8601String(),
      source: 'app_watch',
      imageHash: 'd' * 32,
      analysis: const {'is_food': true},
    ));
    await dao.saveMealThumb(id, Uint8List.fromList([1, 2, 3]));
    expect(await dao.mealThumb(id), [1, 2, 3]);

    await dao.saveMealThumb(id, Uint8List.fromList([9]));
    expect(await dao.mealThumb(id), [9]); // replace, not stack

    await dao.deleteMeal(id);
    expect(await dao.mealThumb(id), isNull,
        reason: 'thumb rides the meal row lifecycle');
  });

  test('photoStatus reads the ledger without writing it', () async {
    expect(await dao.photoStatus('e' * 32), isNull);
    final id = await dao.saveMeal(
      Meal(
        id: 0,
        date: '2026-07-26',
        time: '12:00 PM',
        timestamp: now.toIso8601String(),
        source: 'app_watch',
        imageHash: 'e' * 32,
        analysis: const {'is_food': true},
      ),
      markStatus: IngestionStatus.saved,
    );
    final row = await dao.photoStatus('e' * 32);
    expect(row!.status, IngestionStatus.saved);
    expect(row.mealId, id);
    // Reading must not have created/altered ledger state.
    expect(await dao.reservePhotoHash('e' * 32, source: 'app_watch'), isFalse);
  });
}
