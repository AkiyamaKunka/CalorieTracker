/// Sqflite implementation of the [MealsDao] contract (spec §2).
///
/// All pinned decisions (reservation tree, duplicate window, coercions,
/// export shaping) are delegated to the pure functions in meals_logic.dart;
/// this file only does SQL plumbing and transaction boundaries.
library;

import 'dart:convert';
import 'dart:typed_data';

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
        // finiteAnalysis: jsonEncode throws on NaN/Infinity (Python's
        // json.dumps doesn't) — a model reply carrying 1e400 must save
        // with the safeNumber fallback, not silently drop the meal.
        'analysis': jsonEncode(finiteAnalysis(meal.analysis)),
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
      {'analysis': jsonEncode(finiteAnalysis(analysis)), 'corrected': 1},
      where: 'id = ? AND chat_id = ?',
      whereArgs: [mealId, localChatId],
    );
  }

  @override
  Future<void> updateMealFields(int mealId,
      {Map<String, dynamic>? analysis, String? date, String? time}) async {
    final values = <String, Object?>{'corrected': 1};
    if (analysis != null) {
      values['analysis'] = jsonEncode(finiteAnalysis(analysis));
    }
    if (date != null) values['date'] = date;
    if (time != null) values['time'] = time;
    // timestamp is NOT touched: it records when the row was ingested, while
    // date/time are the user-facing meal moment (spec §2.2).
    await _db.update(
      'meals',
      values,
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
      // App-only thumb rides the meal row's lifecycle.
      await txn.delete('meal_thumbs',
          where: 'meal_id = ?', whereArgs: [mealId]);
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

  /// A 'processing' row younger than this may belong to a LIVE run in the
  /// other isolate (the periodic background job vs. the foreground app) —
  /// sweeping it would double-analyze and double-log the photo. Worst-case
  /// legitimate hold is ~5 min (3 attempts × 90 s deadline + backoffs).
  static const Duration staleProcessingAge = Duration(minutes: 15);

  @override
  Future<void> reclaimStaleProcessing() async {
    // Spec §2.3 app simplification, adjusted for two realities the server
    // never had: (1) EMUI-class OSes routinely kill the process mid-
    // analysis, and (2) a background isolate may hold live reservations
    // while this launch sweep runs. Only rows stale for [staleProcessingAge]
    // are touched. AUTOMATED-watch rows are RELEASED (deleted) so the next
    // scan re-offers the photo — marking them 'failed' silently dropped
    // every interrupted photo forever (watch intake never reclaims failed;
    // observed live 2026-07-23: three of the user's photos burned this way).
    final cutoff =
        _clock().subtract(staleProcessingAge).toIso8601String();
    await _db.delete(
      'photo_ingestions',
      where: 'chat_id = ? AND status = ? AND source = ? AND last_seen_at < ?',
      whereArgs: [
        localChatId,
        IngestionStatus.processing.name,
        MealSource.appWatch,
        cutoff,
      ],
    );
    // DELIBERATE rows (picker/share) keep the spec behavior: mark 'failed'
    // so the user-visible retry path owns them.
    await _db.update(
      'photo_ingestions',
      {
        'status': IngestionStatus.failed.name,
        'meal_id': null,
        'last_seen_at': _clock().toIso8601String(),
      },
      where: 'chat_id = ? AND status = ? AND last_seen_at < ?',
      whereArgs: [localChatId, IngestionStatus.processing.name, cutoff],
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
  Future<void> saveBodyWeight(String date, double kg,
      {String source = 'nl'}) async {
    // Spec §7.1/§4.7: one canonical weigh-in per user-local day; re-log
    // overwrites via upsert ON CONFLICT(chat_id, date) keeping the row id
    // (database.py:744-762). Source: 'nl' from the executor (spec §4.7),
    // 'manual' from the Body page (§9 — the server's weigh-ins only ever
    // arrive through chat).
    await _db.rawInsert(
      '''
INSERT INTO body_weight (chat_id, date, weight_kg, source, note, logged_at)
VALUES (?, ?, ?, ?, '', ?)
ON CONFLICT(chat_id, date) DO UPDATE SET
    weight_kg = excluded.weight_kg,
    source = excluded.source,
    logged_at = excluded.logged_at
''',
      [localChatId, date, kg, source, _clock().toIso8601String()],
    );
  }

  @override
  Future<List<WeightEntry>> listBodyWeights(
      String startDate, String endDate) async {
    final rows = await _db.query('body_weight',
        where: 'chat_id = ? AND date >= ? AND date <= ?',
        whereArgs: [localChatId, startDate, endDate],
        orderBy: 'date ASC');
    return [
      for (final r in rows)
        WeightEntry(r['date'] as String, (r['weight_kg'] as num).toDouble(),
            source: (r['source'] as String?) ?? 'manual'),
    ];
  }

  @override
  Future<void> deleteBodyWeight(String date) async {
    await _db.delete('body_weight',
        where: 'chat_id = ? AND date = ?', whereArgs: [localChatId, date]);
  }

  @override
  Future<void> saveBodyMeasurements(String date,
      {double? waistCm, double? chestCm, double? hipCm}) async {
    // Full-row upsert, nulls included: the sheet is prefilled with the
    // existing row, so this is last-write-wins on the whole day (see the
    // contract doc). A row with nothing in it is a delete, not a save —
    // empty rows would render as blank history entries.
    if (waistCm == null && chestCm == null && hipCm == null) {
      await deleteBodyMeasurements(date);
      return;
    }
    await _db.rawInsert(
      '''
INSERT INTO body_measurements (chat_id, date, waist_cm, chest_cm, hip_cm, note, logged_at)
VALUES (?, ?, ?, ?, ?, '', ?)
ON CONFLICT(chat_id, date) DO UPDATE SET
    waist_cm = excluded.waist_cm,
    chest_cm = excluded.chest_cm,
    hip_cm = excluded.hip_cm,
    logged_at = excluded.logged_at
''',
      [localChatId, date, waistCm, chestCm, hipCm, _clock().toIso8601String()],
    );
  }

  @override
  Future<List<BodyMeasurements>> listBodyMeasurements(
      String startDate, String endDate) async {
    final rows = await _db.query('body_measurements',
        where: 'chat_id = ? AND date >= ? AND date <= ?',
        whereArgs: [localChatId, startDate, endDate],
        orderBy: 'date ASC');
    return [
      for (final r in rows)
        BodyMeasurements(r['date'] as String,
            waistCm: (r['waist_cm'] as num?)?.toDouble(),
            chestCm: (r['chest_cm'] as num?)?.toDouble(),
            hipCm: (r['hip_cm'] as num?)?.toDouble()),
    ];
  }

  @override
  Future<void> deleteBodyMeasurements(String date) async {
    await _db.delete('body_measurements',
        where: 'chat_id = ? AND date = ?', whereArgs: [localChatId, date]);
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
  Future<({IngestionStatus status, int? mealId})?> photoStatus(
      String imageHash) async {
    final hash = normalizeImageHash(imageHash);
    if (hash.isEmpty) return null;
    final rows = await _db.query(
      'photo_ingestions',
      columns: ['status', 'meal_id'],
      where: 'chat_id = ? AND image_hash = ?',
      whereArgs: [localChatId, hash],
      limit: 1,
    );
    if (rows.isEmpty) return null;
    final raw = (rows.first['status'] as String?) ?? '';
    final status = IngestionStatus.values
        .where((v) => v.name == raw)
        .firstOrNull;
    // An unknown status string (foreign import, future version) reads as
    // processing: "in flight, do not touch" is the conservative bucket.
    return (
      status: status ?? IngestionStatus.processing,
      mealId: rows.first['meal_id'] as int?
    );
  }

  @override
  Future<void> saveMealThumb(int mealId, Uint8List jpeg) async {
    if (jpeg.isEmpty) return;
    await _db.transaction((txn) async {
      // The thumb is written AFTER the meal insert, outside that
      // transaction — a user deleting the meal in between must not leave
      // an immortal orphan blob (nothing else ever cleans this table).
      final exists = await txn.query('meals',
          columns: ['id'],
          where: 'id = ? AND chat_id = ?',
          whereArgs: [mealId, localChatId],
          limit: 1);
      if (exists.isEmpty) return;
      await txn.insert(
        'meal_thumbs',
        {'meal_id': mealId, 'jpeg': jpeg},
        conflictAlgorithm: ConflictAlgorithm.replace,
      );
    });
  }

  @override
  Future<Uint8List?> mealThumb(int mealId) async {
    final rows = await _db.query('meal_thumbs',
        columns: ['jpeg'], where: 'meal_id = ?', whereArgs: [mealId], limit: 1);
    if (rows.isEmpty) return null;
    final blob = rows.first['jpeg'];
    if (blob is Uint8List && blob.isNotEmpty) return blob;
    return null;
  }

  @override
  Future<String> exportJson() async {
    final tables = <String, List<Map<String, Object?>>>{};
    for (final table in kExportTables) {
      tables[table] = await _db.query(table);
    }
    return jsonEncode(buildExportEnvelope(tables, _clock()));
  }

  @override
  Future<ImportSummary> importJson(String json) async {
    final envelope = parseExportEnvelope(json); // throws FormatException
    final tables = envelope.tables;
    final added = <String, int>{};
    final skipped = <String, int>{};

    await _db.transaction((txn) async {
      for (final table in kExportTables) {
        final rows = tables[table];
        if (rows == null) continue;
        var a = 0, s = 0;
        for (final raw in rows) {
          final row = Map<String, Object?>.from(raw);
          // The row's own id must NOT be reused: this DB has its own
          // autoincrement sequence, and a colliding id would either fail
          // or overwrite a real meal. meal_id references are dropped for
          // the same reason (the ledger row still records the status).
          row.remove('id');
          if (table == 'photo_ingestions') row['meal_id'] = null;
          if (await _importRowExists(txn, table, row)) {
            s++;
            continue;
          }
          try {
            // OR IGNORE swallows EVERY constraint violation (NOT NULL
            // included) and returns 0 instead of throwing — counting
            // unconditionally would report rows that never landed, and
            // this summary is the user's only evidence the transfer
            // worked.
            final rowId = await txn.insert(table, row,
                conflictAlgorithm: ConflictAlgorithm.ignore);
            if (rowId > 0) {
              a++;
            } else {
              s++;
            }
          } catch (_) {
            // A row whose shape does not fit this schema version is
            // skipped, never fatal: a partial import beats none.
            s++;
          }
        }
        added[table] = a;
        skipped[table] = s;
      }
    });
    return ImportSummary(added, skipped, exportedAt: envelope.exportedAt);
  }

  /// Identity per table — the dedup rule that makes re-importing the same
  /// file a no-op instead of doubling the user's calories.
  Future<bool> _importRowExists(
      DatabaseExecutor txn, String table, Map<String, Object?> row) async {
    // A photoless meal that was EDITED after export no longer matches its
    // exported analysis text, so the analysis-based identity below missed
    // it and an old backup re-imported the stale copy as a second meal
    // (pressure-test find, 2026-08-03). Every edit path sets corrected=1
    // and never touches timestamp, so a corrected row sharing this row's
    // ingestion timestamp IS this meal, post-edit. The analysis-based
    // check stays first: it distinguishes two DIFFERENT same-second
    // text meals (server timestamps are second-resolution).
    if (table == 'meals' &&
        !(((row['image_hash'] as String?)?.isNotEmpty ?? false)) &&
        row['timestamp'] != null) {
      final edited = await txn.query('meals',
          columns: const ['id'],
          where: 'chat_id = ? AND timestamp = ? AND '
              'IFNULL(image_hash, \'\') = \'\' AND corrected = 1',
          whereArgs: [row['chat_id'], row['timestamp']],
          limit: 1);
      if (edited.isNotEmpty) return true;
    }
    final (where, args) = switch (table) {
      // A PHOTO meal's identity is its photo (chat+hash): editing its
      // numbers must NOT make an older backup re-import it as a second
      // meal. A PHOTOLESS meal has no such anchor, so it falls back to
      // date+time+analysis — which is also what keeps two different
      // same-minute text meals distinct.
      'meals' => ((row['image_hash'] as String?)?.isNotEmpty ?? false)
          ? (
              'chat_id = ? AND image_hash = ?',
              [row['chat_id'], row['image_hash']]
            )
          : (
              'chat_id = ? AND date = ? AND time = ? AND '
                  'IFNULL(image_hash, \'\') = \'\' AND analysis = ?',
              [row['chat_id'], row['date'], row['time'], row['analysis']]
            ),
      'photo_ingestions' => (
          'chat_id = ? AND image_hash = ?',
          [row['chat_id'], row['image_hash']]
        ),
      'body_weight' => (
          'chat_id = ? AND date = ?',
          [row['chat_id'], row['date']]
        ),
      // Same one-row-per-day identity as body_weight.
      'body_measurements' => (
          'chat_id = ? AND date = ?',
          [row['chat_id'], row['date']]
        ),
      // NOT date alone: activities allows many rows per day (UNIQUE is
      // chat_id+source+external_id, and manual rows carry a NULL
      // external_id so each save inserts). Keying on the day would have
      // collapsed a morning walk and an evening run into one.
      // NOT date alone: activities allows many rows per day (UNIQUE is
      // chat_id+source+external_id, and manual rows carry a NULL
      // external_id so each save inserts). Keying on the day would have
      // collapsed a morning walk and an evening run into one. logged_at
      // is the per-row stamp that makes two same-day manual rows distinct.
      'activities' => (
          'chat_id = ? AND date = ? AND IFNULL(source, \'\') = ? AND '
              'IFNULL(external_id, \'\') = ? AND '
              'IFNULL(logged_at, \'\') = ? AND '
              'IFNULL(active_calories, -1) = ? AND '
              'IFNULL(distance_km, -1) = ? AND IFNULL(raw, \'\') = ?',
          [
            row['chat_id'],
            row['date'],
            row['source'] ?? '',
            row['external_id'] ?? '',
            row['logged_at'] ?? '',
            // The NUMBERS too: two manual saves in the same second share a
            // logged_at, and a walk is not a run.
            row['active_calories'] ?? -1,
            row['distance_km'] ?? -1,
            row['raw'] ?? '',
          ]
        ),
      'fitness_profile' => ('chat_id = ?', [row['chat_id']]),
      'workouts' => (
          'chat_id = ? AND date = ? AND logged_at = ? AND '
              'IFNULL(workout_type, \'\') = ?',
          [
            row['chat_id'],
            row['date'],
            row['logged_at'],
            row['workout_type'] ?? '',
          ]
        ),
      _ => ('1 = 0', const <Object?>[]),
    };
    final hit = await txn.query(table,
        columns: ['1'], where: where, whereArgs: args, limit: 1);
    return hit.isNotEmpty;
  }
}
