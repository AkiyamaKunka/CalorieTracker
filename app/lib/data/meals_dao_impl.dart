/// Sqflite implementation of the [MealsDao] contract (spec §2).
///
/// All pinned decisions (reservation tree, duplicate window, coercions,
/// export shaping) are delegated to the pure functions in meals_logic.dart;
/// this file only does SQL plumbing and transaction boundaries.
library;

import 'dart:convert';

import 'package:sqflite/sqflite.dart';

import '../core/coerce.dart';
import '../core/contracts.dart';
import 'db.dart';
import 'meals_logic.dart';

/// Integration factory: opens the singleton database and returns the DAO.
Future<MealsDao> createMealsDao() async => SqfliteMealsDao(await appDatabase());

class SqfliteMealsDao implements MealsDao {
  SqfliteMealsDao(this._db, {DateTime Function()? clock})
      : _clock = clock ?? DateTime.now;

  final Database _db;

  /// Injectable clock so time-window behavior is testable on-device.
  final DateTime Function() _clock;

  // ---------------------------------------------------------------- meals

  @override
  Future<int> saveMeal(Meal meal, {IngestionStatus? markStatus}) {
    final hash = normalizeImageHash(meal.imageHash);
    // Spec §2.3: the meal INSERT and the ledger mark are ONE transaction
    // (database.py:158-189, the atomic save).
    return _db.transaction((txn) async {
      final id = await txn.insert('meals', {
        'chat_id': meal.chatId,
        'date': meal.date,
        'time': meal.time,
        'timestamp': meal.timestamp,
        'source': meal.source,
        'image_hash': hash,
        'file_id': meal.fileId,
        // Spec §2.2: analysis is stored as JSON text (json.dumps parity).
        'analysis': jsonEncode(meal.analysis),
        'corrected': meal.corrected ? 1 : 0,
      });
      // Spec §2.2: empty-after-normalization hash → ledger writes skipped.
      if (markStatus != null && hash.isNotEmpty) {
        await _upsertLedger(
          txn,
          hash,
          markStatus,
          // meal_id is set only by the 'saved' mark (spec §2.3 status table).
          mealId: markStatus == IngestionStatus.saved ? id : null,
          source: meal.source,
        );
      }
      return id;
    });
  }

  @override
  Future<void> updateMealAnalysis(int mealId, Map<String, dynamic> analysis) async {
    // Spec §2.4: rewrites analysis JSON and sets corrected=1 unconditionally
    // (database.py:567-576), keyed by DB id + chat id.
    await _db.update(
      'meals',
      {'analysis': jsonEncode(analysis), 'corrected': 1},
      where: 'id = ? AND chat_id = ?',
      whereArgs: [mealId, localChatId],
    );
  }

  @override
  Future<void> deleteMeal(int mealId) async {
    // Spec §2.4: delete the row; a non-empty image_hash tombstones the ledger
    // row to status='deleted', meal_id=NULL (database.py:578-597) — this is
    // what stops the §6 backfill scan from resurrecting the photo.
    await _db.transaction((txn) async {
      final rows = await txn.query(
        'meals',
        columns: ['image_hash'],
        where: 'id = ? AND chat_id = ?',
        whereArgs: [mealId, localChatId],
        limit: 1,
      );
      if (rows.isEmpty) return;
      final hash = normalizeImageHash(rows.first['image_hash'] as String?);
      await txn.delete(
        'meals',
        where: 'id = ? AND chat_id = ?',
        whereArgs: [mealId, localChatId],
      );
      if (hash.isNotEmpty) {
        await txn.update(
          'photo_ingestions',
          {'status': IngestionStatus.deleted.name, 'meal_id': null},
          where: 'chat_id = ? AND image_hash = ?',
          whereArgs: [localChatId, hash],
        );
      }
    });
  }

  @override
  Future<List<Meal>> mealsBetween(String startDate, String endDate) async {
    final rows = await _db.query(
      'meals',
      where: 'chat_id = ? AND date >= ? AND date <= ?',
      whereArgs: [localChatId, startDate, endDate],
      orderBy: 'timestamp ASC',
    );
    // Readers get every row (views filter is_food themselves); analysis is
    // coerced per spec §2.2 inside mealFromRow.
    return rows.map(mealFromRow).toList();
  }

  @override
  Future<List<Meal>> recentMeals({int days = 7}) async {
    // Spec §1.2: last N calendar days INCLUDING today, is_food only (Python
    // truthiness), ordered by timestamp ascending (database.py:439-444).
    final now = _clock();
    final rows = await _db.query(
      'meals',
      where: 'chat_id = ? AND date >= ? AND date <= ?',
      whereArgs: [localChatId, windowStartDate(now, days), isoDate(now)],
      orderBy: 'timestamp ASC',
    );
    return rows
        .map(mealFromRow)
        .where((m) => isFoodTruthy(m.analysis['is_food']))
        .toList();
  }

  // ------------------------------------------------------- photo ledger

  @override
  Future<bool> isDuplicatePhoto(String imageHash) async {
    final hash = normalizeImageHash(imageHash);
    if (hash.isEmpty) return false;
    // Spec §2.3 pre-reservation dedup: same hash, a meal from TODAY, within
    // the 5-minute window (telegram_bot.py:2854-2872).
    final now = _clock();
    final rows = await _db.query(
      'meals',
      columns: ['timestamp'],
      where: 'chat_id = ? AND image_hash = ? AND date = ?',
      whereArgs: [localChatId, hash, isoDate(now)],
    );
    return hasDuplicateInWindow(rows, now);
  }

  @override
  Future<bool> reservePhotoHash(String imageHash,
      {required String source, bool reclaimDeliberate = false}) {
    final hash = normalizeImageHash(imageHash);
    // Spec §2.3 step 1: empty hash → nothing to reserve.
    if (hash.isEmpty) return Future.value(true);
    // One transaction stands in for the server's row-locked decision tree —
    // sufficient in a single-process app (spec §2.3 app simplification).
    return _db.transaction((txn) async {
      final mealRows = await txn.query(
        'meals',
        columns: ['id'],
        where: 'chat_id = ? AND image_hash = ?',
        whereArgs: [localChatId, hash],
        limit: 1,
      );
      final ledgerRows = await txn.query(
        'photo_ingestions',
        where: 'chat_id = ? AND image_hash = ?',
        whereArgs: [localChatId, hash],
        limit: 1,
      );
      final now = _clock();
      final ledger = ledgerRows.isEmpty ? null : ledgerRows.first;
      final decision = decideReservation(
        hashEmpty: false,
        mealRowExists: mealRows.isNotEmpty,
        ledgerStatus:
            ledger == null ? null : (ledger['status'] as String?) ?? '',
        ledgerLastSeenAt: ledger == null
            ? null
            : DateTime.tryParse((ledger['last_seen_at'] as String?) ?? ''),
        reclaimDeliberate: reclaimDeliberate,
        now: now,
      );
      switch (decision) {
        case ReserveDecision.allowNoop:
          return true;
        case ReserveDecision.refuse:
          return false;
        case ReserveDecision.reclaim:
          // database.py:242-254: row → processing, meal_id=NULL,
          // last_seen_at=now, source updated.
          await txn.update(
            'photo_ingestions',
            {
              'status': IngestionStatus.processing.name,
              'meal_id': null,
              'last_seen_at': now.toIso8601String(),
              'source': source,
            },
            where: 'chat_id = ? AND image_hash = ?',
            whereArgs: [localChatId, hash],
          );
          return true;
        case ReserveDecision.insert:
          // database.py:259-265: fresh reservation.
          await txn.insert('photo_ingestions', {
            'chat_id': localChatId,
            'image_hash': hash,
            'first_seen_at': now.toIso8601String(),
            'last_seen_at': now.toIso8601String(),
            'source': source,
            'status': IngestionStatus.processing.name,
            'meal_id': null,
          });
          return true;
      }
    });
  }

  @override
  Future<void> markPhotoHash(String imageHash, IngestionStatus status,
      {int? mealId}) async {
    final hash = normalizeImageHash(imageHash);
    if (hash.isEmpty) return; // spec §2.2: no hash → ledger no-op
    await _upsertLedger(_db, hash, status, mealId: mealId);
  }

  @override
  Future<void> releasePhotoHash(String imageHash) async {
    final hash = normalizeImageHash(imageHash);
    if (hash.isEmpty) return;
    // Spec §2.3: release deletes ONLY rows still in 'processing'
    // (database.py:304-315) — never a saved/skipped/failed/deleted record.
    await _db.delete(
      'photo_ingestions',
      where: 'chat_id = ? AND image_hash = ? AND status = ?',
      whereArgs: [localChatId, hash, IngestionStatus.processing.name],
    );
  }

  @override
  Future<void> reclaimStaleProcessing() async {
    // Spec §2.3 app simplification: in a single-process app, any 'processing'
    // row at launch is a crashed run. Mark it 'failed' — the original photo
    // stays retrievable via retry-failed / deliberate re-add, and the auto
    // intake still won't re-log it (the 6-hour timer has no app role).
    await _db.update(
      'photo_ingestions',
      {
        'status': IngestionStatus.failed.name,
        'meal_id': null,
        'last_seen_at': _clock().toIso8601String(),
      },
      where: 'chat_id = ? AND status = ?',
      whereArgs: [localChatId, IngestionStatus.processing.name],
    );
  }

  /// Shared ledger upsert: UPDATE the (chat_id, image_hash) row; if absent,
  /// INSERT it. Runs on a [DatabaseExecutor] so saveMeal can call it inside
  /// its transaction. A null [mealId] clears the column (tombstone rule).
  Future<void> _upsertLedger(
    DatabaseExecutor executor,
    String hash,
    IngestionStatus status, {
    int? mealId,
    String? source,
  }) async {
    final now = _clock().toIso8601String();
    final updated = await executor.update(
      'photo_ingestions',
      {
        'status': status.name,
        'meal_id': mealId,
        'last_seen_at': now,
        'source': ?source,
      },
      where: 'chat_id = ? AND image_hash = ?',
      whereArgs: [localChatId, hash],
    );
    if (updated == 0) {
      await executor.insert('photo_ingestions', {
        'chat_id': localChatId,
        'image_hash': hash,
        'first_seen_at': now,
        'last_seen_at': now,
        'source': source,
        'status': status.name,
        'meal_id': mealId,
      });
    }
  }

  // ------------------------------------------------------------- fitness

  @override
  Future<void> saveBodyWeight(String date, double kg) async {
    // Spec §7.1/§4.7: one canonical weigh-in per user-local day; re-log
    // overwrites via upsert ON CONFLICT(chat_id, date) keeping the row id
    // (database.py:744-762). Source 'nl' — the NL executor is the only
    // phase-1 writer (spec §4.7 app note).
    await _db.rawInsert(
      '''
INSERT INTO body_weight (chat_id, date, weight_kg, source, note, logged_at)
VALUES (?, ?, ?, ?, '', ?)
ON CONFLICT(chat_id, date) DO UPDATE SET
    weight_kg = excluded.weight_kg,
    source = excluded.source,
    logged_at = excluded.logged_at
''',
      [localChatId, date, kg, 'nl', _clock().toIso8601String()],
    );
  }

  @override
  Future<void> saveActivity(String date,
      {num? activeCalories, int? steps, double? distanceKm}) async {
    // Spec §4.8: one activities row, source/activity_type 'manual',
    // kcal/km stored as NULL when 0, steps ride in raw={"steps": n}
    // (telegram_bot.py:4142-4147). external_id stays NULL so manual rows
    // always insert (spec §2.1 UNIQUE-with-NULL rule).
    final kcal = (activeCalories ?? 0) > 0 ? activeCalories : null;
    final km = (distanceKm ?? 0) > 0 ? distanceKm : null;
    final stepCount = (steps ?? 0) > 0 ? steps : null;
    await _db.insert('activities', {
      'chat_id': localChatId,
      'date': date,
      'activity_type': 'manual',
      'source': 'manual',
      'active_calories': kcal,
      'distance_km': km,
      'raw': stepCount == null ? null : jsonEncode({'steps': stepCount}),
      'notes': '',
      'logged_at': _clock().toIso8601String(),
    });
  }

  // -------------------------------------------------------------- export

  @override
  Future<String> exportJson() async {
    final tables = <String, List<Map<String, Object?>>>{};
    for (final table in kExportTables) {
      tables[table] = await _db.query(table);
    }
    return jsonEncode(buildExportEnvelope(tables, _clock()));
  }
}
