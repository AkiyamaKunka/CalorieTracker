// Ports the Python captured_at pinning tests (spec §6.3):
// tests/test_upload_photo.py::test_captured_at_survives_hash_prefixed_queue_name
// tests/test_telegram_bot.py::test_parse_captured_at_future_boundary_inclusive_at_plus_1h
// tests/test_telegram_bot.py::test_parse_captured_at_age_boundary_inclusive_at_max_age
import 'package:calorie_tracker/services/photo/filename_dates.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  // Same frozen clock as the Python boundary tests.
  final frozenNow = DateTime(2026, 7, 15, 12, 0, 0);

  group('capturedAtFromFilename', () {
    test('plain camera filename parses as local wall time', () {
      expect(capturedAtFromFilename('IMG_20260716_193042.jpg'),
          DateTime(2026, 7, 16, 19, 30, 42));
    });

    test('hash-prefixed queue names survive (Python pin)', () {
      expect(capturedAtFromFilename('a3f9c2d81b04__IMG_20260715_193042.jpg'),
          DateTime(2026, 7, 15, 19, 30, 42));
      // Worst case: all-digit hash prefix directly before the date.
      expect(capturedAtFromFilename('123456789012__20260715_193042.jpg'),
          DateTime(2026, 7, 15, 19, 30, 42));
    });

    test('filename starting with the date parses (start-of-string boundary)',
        () {
      expect(capturedAtFromFilename('20260715_193042.jpg'),
          DateTime(2026, 7, 15, 19, 30, 42));
    });

    test('digit run flowing into the date yields no phantom date', () {
      // 9 digits before '_': no non-digit boundary anywhere → no match.
      expect(capturedAtFromFilename('920260715_193042.jpg'), isNull);
    });

    test('junk names return null', () {
      expect(capturedAtFromFilename(null), isNull);
      expect(capturedAtFromFilename(''), isNull);
      expect(capturedAtFromFilename('meal.jpg'), isNull);
      expect(capturedAtFromFilename('IMG_2026_0716.jpg'), isNull);
      expect(capturedAtFromFilename('IMG_20260716-193042.jpg'), isNull);
      expect(capturedAtFromFilename('IMG_2026071_193042.jpg'), isNull);
    });

    test('invalid datetimes rejected (strptime parity, no Dart rollover)', () {
      expect(capturedAtFromFilename('IMG_20261332_120000.jpg'), isNull); // m 13
      expect(capturedAtFromFilename('IMG_20260231_120000.jpg'), isNull); // Feb 31
      expect(capturedAtFromFilename('IMG_20260716_250000.jpg'), isNull); // h 25
      expect(capturedAtFromFilename('IMG_20260716_196042.jpg'), isNull); // min 60
      // Leap-day sanity: 2024-02-29 valid, 2026-02-29 not.
      expect(capturedAtFromFilename('IMG_20240229_080000.jpg'),
          DateTime(2024, 2, 29, 8, 0, 0));
      expect(capturedAtFromFilename('IMG_20260229_080000.jpg'), isNull);
    });
  });

  group('validateCapturedAt window (§6.3: <=45d old, <=+1h future)', () {
    test('future boundary inclusive at exactly +1h, rejects +1h1s', () {
      final atEdge = frozenNow.add(const Duration(hours: 1));
      expect(validateCapturedAt(atEdge, now: frozenNow), atEdge);
      expect(
          validateCapturedAt(atEdge.add(const Duration(seconds: 1)),
              now: frozenNow),
          isNull);
    });

    test('age boundary inclusive at exactly 45 days, rejects 45d1s', () {
      final atEdge = frozenNow.subtract(const Duration(days: 45));
      expect(validateCapturedAt(atEdge, now: frozenNow), atEdge);
      expect(
          validateCapturedAt(atEdge.subtract(const Duration(seconds: 1)),
              now: frozenNow),
          isNull);
    });

    test('null passes through as null', () {
      expect(validateCapturedAt(null, now: frozenNow), isNull);
    });

    test('maxAgeDays clamps to 1..365 (§8 knob range)', () {
      final twoDaysOld = frozenNow.subtract(const Duration(days: 2));
      // 0 clamps to 1 → 2-day-old value rejected.
      expect(validateCapturedAt(twoDaysOld, now: frozenNow, maxAgeDays: 0),
          isNull);
      final old400 = frozenNow.subtract(const Duration(days: 400));
      // 9999 clamps to 365 → 400-day-old value still rejected.
      expect(validateCapturedAt(old400, now: frozenNow, maxAgeDays: 9999),
          isNull);
    });
  });

  group('deriveCapturedAt priority (§6.3: filename, then asset date)', () {
    test('valid filename timestamp beats the asset create date', () {
      final assetDate = DateTime(2026, 7, 10, 9, 0, 0);
      expect(
          deriveCapturedAt(
              fileName: 'IMG_20260714_193042.jpg',
              assetCreateDate: assetDate,
              now: frozenNow),
          DateTime(2026, 7, 14, 19, 30, 42));
    });

    test('junk filename falls back to the asset create date', () {
      final assetDate = DateTime(2026, 7, 10, 9, 0, 0);
      expect(
          deriveCapturedAt(
              fileName: 'meal.jpg', assetCreateDate: assetDate, now: frozenNow),
          assetDate);
    });

    test('stale filename timestamp falls back to a valid asset date', () {
      final assetDate = DateTime(2026, 7, 10, 9, 0, 0);
      expect(
          deriveCapturedAt(
              fileName: 'IMG_20250101_120000.jpg', // > 45 days old
              assetCreateDate: assetDate,
              now: frozenNow),
          assetDate);
    });

    test('asset date is validated under the same window', () {
      final stale = frozenNow.subtract(const Duration(days: 46));
      expect(
          deriveCapturedAt(
              fileName: 'meal.jpg', assetCreateDate: stale, now: frozenNow),
          isNull);
      final future = frozenNow.add(const Duration(hours: 2));
      expect(
          deriveCapturedAt(
              fileName: 'meal.jpg', assetCreateDate: future, now: frozenNow),
          isNull);
    });

    test('nothing valid → null (fallback = intake-time dating downstream)',
        () {
      expect(deriveCapturedAt(fileName: 'meal.jpg', now: frozenNow), isNull);
    });
  });
}
