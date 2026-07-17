/// Pure decision logic for the data layer (no sqflite dependency).
///
/// Everything with pinned semantics that does not need a live Database lives
/// here so it can be unit-tested off-device: the reservation decision tree
/// (spec §2.3), the 5-minute duplicate window (spec §2.3), stored-analysis
/// coercion (spec §2.2), Python-truthiness is_food filtering (spec §3.5),
/// date windows (spec §1.2/§5.3), row→Meal mapping, and export shaping
/// (spec §8 / MealsDao.exportJson).
library;

import 'dart:convert';

import '../core/coerce.dart';
import '../core/contracts.dart';

/// Spec §2: chat_id kept for schema/export parity; one constant local user.
const int localChatId = 1;

/// Spec §2.3: PHOTO_RESERVATION_STALE_SECONDS = 6 h (database.py:11).
const int photoReservationStaleSeconds = 6 * 60 * 60;

/// Spec §2.3: DUPLICATE_WINDOW_MINUTES = 5 (config.py:95).
const int duplicateWindowMinutes = 5;

/// Spec §3.5: Python truthiness for is_food — false, 0, 0.0, '', [], {},
/// null → false; anything else (including non-empty strings, 1, NaN) → true.
/// Consumers filter with raw truthiness, never `== true`.
bool isFoodTruthy(dynamic value) {
  if (value == null) return false;
  if (value is bool) return value;
  // NaN != 0 is true, matching Python where NaN is truthy.
  if (value is num) return value != 0;
  if (value is String) return value.isNotEmpty;
  if (value is List) return value.isNotEmpty;
  if (value is Map) return value.isNotEmpty;
  return true;
}

/// Spec §2.2: stored `analysis` is JSON text; a parse result that is not an
/// object/map coerces to {} — a poison row must never crash a reader.
Map<String, dynamic> coerceStoredAnalysis(dynamic stored) {
  if (stored is! String || stored.isEmpty) return <String, dynamic>{};
  dynamic parsed;
  try {
    parsed = jsonDecode(stored);
  } on FormatException {
    return <String, dynamic>{};
  }
  if (parsed is Map) return Map<String, dynamic>.from(parsed);
  return <String, dynamic>{};
}

/// User-local calendar day as ISO YYYY-MM-DD (spec §2.2 clock simplification:
/// the device clock is the only clock).
String isoDate(DateTime d) =>
    '${d.year.toString().padLeft(4, '0')}-'
    '${d.month.toString().padLeft(2, '0')}-'
    '${d.day.toString().padLeft(2, '0')}';

/// Start date of an inclusive N-calendar-day window ending today
/// (spec §1.2 / §5.3: "last N days including today" → [today−(N−1), today]).
String windowStartDate(DateTime today, int days) =>
    isoDate(DateTime(today.year, today.month, today.day - (days - 1)));

/// Spec §2.3 pre-reservation dedup: given today's meal rows sharing the
/// photo's hash, "same photo again within 5 minutes" is a duplicate. A row
/// with a missing/blank timestamp counts as a duplicate; an unparseable
/// timestamp does not (telegram_bot.py:2854-2872).
bool hasDuplicateInWindow(Iterable<Map<String, Object?>> todaysRows, DateTime now) {
  for (final row in todaysRows) {
    final ts = row['timestamp'];
    if (ts is! String || ts.trim().isEmpty) return true;
    final parsed = DateTime.tryParse(ts);
    if (parsed == null) continue;
    if (now.difference(parsed) < const Duration(minutes: duplicateWindowMinutes)) {
      return true;
    }
  }
  return false;
}

/// Outcome of the reserve_photo_hash decision tree (spec §2.3).
enum ReserveDecision {
  /// Empty hash: nothing to reserve, reservation trivially succeeds.
  allowNoop,

  /// Hash already claimed (meals-table backstop or unreclaimable ledger row).
  refuse,

  /// Existing ledger row flips back to 'processing' (stale or reclaimable).
  reclaim,

  /// No ledger row: insert a fresh 'processing' reservation.
  insert,
}

/// Port of the reserve_photo_hash decision tree (database.py:205-268, spec
/// §2.3), evaluated over a pre-read snapshot; the caller applies the decision
/// inside one transaction. [ledgerStatus] is null when no ledger row exists.
///
/// [reclaimDeliberate] maps the caller policies: a deliberate user re-add
/// reclaims {failed, skipped, deleted} (telegram_bot.py:4669-4674); the
/// automated intake passes false — strict, no reclaim (telegram_bot.py:4882).
ReserveDecision decideReservation({
  required bool hashEmpty,
  required bool mealRowExists,
  required String? ledgerStatus,
  required DateTime? ledgerLastSeenAt,
  required bool reclaimDeliberate,
  required DateTime now,
}) {
  // §2.3 step 1: empty-after-normalization hash means "no hash".
  if (hashEmpty) return ReserveDecision.allowNoop;
  // §2.3 step 2: meals-table backstop catches ledger/meal divergence.
  if (mealRowExists) return ReserveDecision.refuse;
  // §2.3 step 4: no ledger row → fresh reservation.
  if (ledgerStatus == null) return ReserveDecision.insert;
  // §2.3 step 3a: stale 'processing' rows are reclaimable. An unparseable
  // last_seen_at reads as stale — a corrupt row must not block forever.
  if (ledgerStatus == IngestionStatus.processing.name) {
    final last = ledgerLastSeenAt;
    final stale = last == null ||
        now.difference(last) >
            const Duration(seconds: photoReservationStaleSeconds);
    return stale ? ReserveDecision.reclaim : ReserveDecision.refuse;
  }
  // §2.3 step 3b: deliberate re-send may re-log failed/skipped/deleted.
  const deliberateReclaim = {'failed', 'skipped', 'deleted'};
  if (reclaimDeliberate && deliberateReclaim.contains(ledgerStatus)) {
    return ReserveDecision.reclaim;
  }
  return ReserveDecision.refuse;
}

/// Map a meals-table row to the contract [Meal]: corrected 0/1 → bool
/// (database.py:435), analysis coerced per spec §2.2, hash normalized per
/// spec §2.2 (`(hash ?? '').trim().toLowerCase()`).
Meal mealFromRow(Map<String, Object?> row) => Meal(
      id: (row['id'] as num?)?.toInt() ?? 0,
      chatId: (row['chat_id'] as num?)?.toInt() ?? localChatId,
      date: (row['date'] as String?) ?? '',
      time: (row['time'] as String?) ?? '',
      timestamp: (row['timestamp'] as String?) ?? '',
      source: (row['source'] as String?) ?? '',
      imageHash: normalizeImageHash(row['image_hash'] as String?),
      fileId: (row['file_id'] as String?) ?? '',
      analysis: coerceStoredAnalysis(row['analysis']),
      corrected: ((row['corrected'] as num?) ?? 0) != 0,
    );

/// Versioned full-export envelope (MealsDao.exportJson, spec §8): every
/// table's raw rows under a stable version so a future importer can migrate.
Map<String, dynamic> buildExportEnvelope(
  Map<String, List<Map<String, Object?>>> tables,
  DateTime exportedAt,
) =>
    <String, dynamic>{
      'format': 'calorie_tracker_export',
      'version': 1,
      'exported_at': exportedAt.toIso8601String(),
      'tables': tables,
    };
