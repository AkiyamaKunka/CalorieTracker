/// Pinning tests for the data layer's pure decision logic (spec §2, §3.5).
///
/// The SQL plumbing in meals_dao_impl.dart needs a platform sqflite channel,
/// so DB round-trips are exercised on-device; every pinned DECISION —
/// reservation tree, duplicate window, coercions, date windows, export
/// shape — lives in meals_logic.dart and is verified here.
library;

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/data/meals_logic.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  final now = DateTime(2026, 7, 17, 12, 0, 0);

  group('reservation decision tree (spec §2.3, database.py:205-268)', () {
    ReserveDecision decide({
      bool hashEmpty = false,
      bool mealRowExists = false,
      String? ledgerStatus,
      DateTime? lastSeen,
      bool reclaimDeliberate = false,
    }) =>
        decideReservation(
          hashEmpty: hashEmpty,
          mealRowExists: mealRowExists,
          ledgerStatus: ledgerStatus,
          ledgerLastSeenAt: lastSeen,
          reclaimDeliberate: reclaimDeliberate,
          now: now,
        );

    test('empty hash is a trivial success (nothing to reserve)', () {
      expect(decide(hashEmpty: true), ReserveDecision.allowNoop);
      // The backstop must not even matter for an empty hash.
      expect(decide(hashEmpty: true, mealRowExists: true),
          ReserveDecision.allowNoop);
    });

    test('meals-table backstop refuses even without a ledger row', () {
      expect(decide(mealRowExists: true), ReserveDecision.refuse);
      // Backstop wins over an otherwise-reclaimable ledger row.
      expect(
          decide(
              mealRowExists: true,
              ledgerStatus: 'failed',
              reclaimDeliberate: true),
          ReserveDecision.refuse);
    });

    test('no ledger row inserts a fresh processing reservation', () {
      expect(decide(), ReserveDecision.insert);
    });

    test('fresh processing row blocks everyone, deliberate or not', () {
      final fresh = now.subtract(const Duration(minutes: 5));
      expect(decide(ledgerStatus: 'processing', lastSeen: fresh),
          ReserveDecision.refuse);
      expect(
          decide(
              ledgerStatus: 'processing',
              lastSeen: fresh,
              reclaimDeliberate: true),
          ReserveDecision.refuse);
    });

    test('processing goes stale strictly after 6 hours', () {
      final exactly6h =
          now.subtract(const Duration(seconds: photoReservationStaleSeconds));
      final over6h = now.subtract(
          const Duration(seconds: photoReservationStaleSeconds + 1));
      expect(decide(ledgerStatus: 'processing', lastSeen: exactly6h),
          ReserveDecision.refuse);
      expect(decide(ledgerStatus: 'processing', lastSeen: over6h),
          ReserveDecision.reclaim);
    });

    test('processing with unparseable last_seen_at reads as stale', () {
      expect(decide(ledgerStatus: 'processing', lastSeen: null),
          ReserveDecision.reclaim);
    });

    test('deliberate re-add reclaims failed, skipped, and deleted', () {
      for (final status in ['failed', 'skipped', 'deleted']) {
        expect(decide(ledgerStatus: status, reclaimDeliberate: true),
            ReserveDecision.reclaim,
            reason: status);
      }
    });

    test('automated intake is strict: no reclaim of any tombstone', () {
      for (final status in ['failed', 'skipped', 'deleted', 'saved']) {
        expect(decide(ledgerStatus: status), ReserveDecision.refuse,
            reason: status);
      }
    });

    test('saved rows are never reclaimable, even deliberately', () {
      expect(decide(ledgerStatus: 'saved', reclaimDeliberate: true),
          ReserveDecision.refuse);
    });
  });

  group('5-minute duplicate window (spec §2.3, telegram_bot.py:2854-2872)', () {
    test('same-hash meal 4 minutes old is a duplicate', () {
      final rows = [
        {
          'timestamp':
              now.subtract(const Duration(minutes: 4)).toIso8601String(),
        },
      ];
      expect(hasDuplicateInWindow(rows, now), isTrue);
    });

    test('same-hash meal 6 minutes old is not a duplicate', () {
      final rows = [
        {
          'timestamp':
              now.subtract(const Duration(minutes: 6)).toIso8601String(),
        },
      ];
      expect(hasDuplicateInWindow(rows, now), isFalse);
    });

    test('missing or blank timestamp counts as a duplicate', () {
      expect(hasDuplicateInWindow([{'timestamp': null}], now), isTrue);
      expect(hasDuplicateInWindow([{'timestamp': '   '}], now), isTrue);
    });

    test('unparseable timestamp does NOT count as a duplicate', () {
      expect(
          hasDuplicateInWindow([{'timestamp': 'not-a-date'}], now), isFalse);
    });

    test('one stale row plus one fresh row is still a duplicate', () {
      final rows = [
        {
          'timestamp':
              now.subtract(const Duration(hours: 3)).toIso8601String(),
        },
        {
          'timestamp':
              now.subtract(const Duration(minutes: 1)).toIso8601String(),
        },
      ];
      expect(hasDuplicateInWindow(rows, now), isTrue);
    });

    test('no rows means no duplicate', () {
      expect(hasDuplicateInWindow(const [], now), isFalse);
    });
  });

  group('is_food Python truthiness (spec §3.5, database.py:453-461)', () {
    test('falsy: false, 0, 0.0, "", [], {}, null', () {
      for (final v in [false, 0, 0.0, '', <dynamic>[], <String, dynamic>{}, null]) {
        expect(isFoodTruthy(v), isFalse, reason: '$v');
      }
    });

    test('truthy: true, 1, non-empty strings ("yes", even "false"), lists', () {
      for (final v in [true, 1, -1, 0.5, 'yes', 'false', '0', [1], {'a': 1}]) {
        expect(isFoodTruthy(v), isTrue, reason: '$v');
      }
    });

    test('NaN is truthy, as in Python', () {
      expect(isFoodTruthy(double.nan), isTrue);
    });
  });

  group('stored-analysis coercion (spec §2.2, database.py:428-437)', () {
    test('object JSON parses to its map', () {
      expect(coerceStoredAnalysis('{"is_food": true, "total_calories": 450}'),
          {'is_food': true, 'total_calories': 450});
    });

    test('non-object JSON literals coerce to {}', () {
      for (final poison in ['null', '"text"', '[1,2]', '42', 'true']) {
        expect(coerceStoredAnalysis(poison), isEmpty, reason: poison);
      }
    });

    test('invalid JSON and non-string input coerce to {}', () {
      expect(coerceStoredAnalysis('{broken'), isEmpty);
      expect(coerceStoredAnalysis(null), isEmpty);
      expect(coerceStoredAnalysis(7), isEmpty);
      expect(coerceStoredAnalysis(''), isEmpty);
    });
  });

  group('date windows (spec §1.2, §5.3)', () {
    test('isoDate zero-pads', () {
      expect(isoDate(DateTime(2026, 7, 5)), '2026-07-05');
    });

    test('7-day window including today starts at today-6', () {
      expect(windowStartDate(DateTime(2026, 7, 17), 7), '2026-07-11');
    });

    test('window start crosses month boundaries', () {
      expect(windowStartDate(DateTime(2026, 7, 3), 7), '2026-06-27');
    });

    test('1-day window is today itself', () {
      expect(windowStartDate(DateTime(2026, 7, 17), 1), '2026-07-17');
    });
  });

  group('row → Meal mapping (spec §2.2)', () {
    test('corrected 0/1 reads back as bool, hash normalizes, analysis coerces',
        () {
      final meal = mealFromRow({
        'id': 3,
        'chat_id': 1,
        'date': '2026-07-17',
        'time': '07:42 PM',
        'timestamp': '2026-07-17T19:42:00',
        'source': 'camera',
        'image_hash': '  ABCDEF0123  ',
        'file_id': null,
        'analysis': '"poison"',
        'corrected': 1,
      });
      expect(meal.id, 3);
      expect(meal.corrected, isTrue);
      expect(meal.imageHash, 'abcdef0123');
      expect(meal.fileId, '');
      expect(meal.analysis, isEmpty);
    });

    test('null-heavy row degrades to defaults, never throws', () {
      final meal = mealFromRow({'id': 9, 'analysis': null});
      expect(meal.corrected, isFalse);
      expect(meal.date, '');
      expect(meal.imageHash, '');
      expect(meal.analysis, isEmpty);
    });
  });

  group('export envelope shaping (spec §8, MealsDao.exportJson)', () {
    test('versioned envelope carries all tables and rows untouched', () {
      final tables = {
        'meals': [
          {'id': 1, 'analysis': '{}'},
        ],
        'photo_ingestions': <Map<String, Object?>>[],
      };
      final envelope =
          buildExportEnvelope(tables, DateTime(2026, 7, 17, 8, 30));
      expect(envelope['format'], 'calorie_tracker_export');
      expect(envelope['version'], 1);
      expect(envelope['exported_at'], '2026-07-17T08:30:00.000');
      expect(envelope['tables'], same(tables));
      expect((envelope['tables'] as Map)['meals'], hasLength(1));
    });
  });

  group('constants pinned by the spec', () {
    test('spec §2.3 / §2 values', () {
      expect(localChatId, 1);
      expect(photoReservationStaleSeconds, 6 * 60 * 60);
      expect(duplicateWindowMinutes, 5);
    });

    test('IngestionStatus enum names match the ledger status strings', () {
      expect(IngestionStatus.values.map((s) => s.name),
          ['processing', 'saved', 'skipped', 'failed', 'deleted']);
    });
  });
}
