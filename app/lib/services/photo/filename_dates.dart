/// captured_at derivation + validation (spec §6.3).
///
/// Ports upload_photo.py `_captured_at_from_filename` and telegram_bot.py
/// `_parse_captured_at` (the validation window). Pinned by the Python tests
/// test_captured_at_survives_hash_prefixed_queue_name,
/// test_parse_captured_at_future_boundary_inclusive_at_plus_1h and
/// test_parse_captured_at_age_boundary_inclusive_at_max_age.
library;

import '../../core/shared_generated.dart';

/// Port of `_FILENAME_TS_RE` (upload_photo.py): 8 digits, '_', 6 digits,
/// preceded by start-of-string or a NON-digit. The boundary rule means a
/// digit run like "920260715_193042" yields no phantom date, while a queue
/// name "123456789012__IMG_20260715_193042.jpg" still parses (spec §6.3).
final RegExp _filenameTsRe = RegExp(r'(?:^|[^0-9])(\d{8})_(\d{6})');

/// Device-local wall-clock capture time from a camera filename
/// (IMG_YYYYMMDD_HHMMSS...), or null when absent/invalid. UNVALIDATED —
/// callers apply [validateCapturedAt] before trusting it.
DateTime? capturedAtFromFilename(String? name) {
  final match = _filenameTsRe.firstMatch(name ?? '');
  if (match == null) return null;
  final d = match.group(1)!;
  final t = match.group(2)!;
  final year = int.parse(d.substring(0, 4));
  final month = int.parse(d.substring(4, 6));
  final day = int.parse(d.substring(6, 8));
  final hour = int.parse(t.substring(0, 2));
  final minute = int.parse(t.substring(2, 4));
  final second = int.parse(t.substring(4, 6));
  final parsed = DateTime(year, month, day, hour, minute, second);
  // strptime parity: Python rejects out-of-range components (month 13,
  // Feb 31); Dart's DateTime silently rolls them over, so require an exact
  // component round-trip instead.
  final valid = parsed.year == year &&
      parsed.month == month &&
      parsed.day == day &&
      parsed.hour == hour &&
      parsed.minute == minute &&
      parsed.second == second;
  return valid ? parsed : null;
}

/// Port of the string half of telegram_bot.py `_parse_captured_at`: strict
/// `YYYY-MM-DD HH:MM:SS` (strptime "%Y-%m-%d %H:%M:%S" — surrounding
/// whitespace tolerated, ISO "T" separators / date-only / missing seconds
/// are NOT), else null. Pinned by shared/vectors/captured_at.json — a lenient
/// DateTime.parse here would accept shapes the server rejects.
final RegExp _capturedAtStrRe =
    RegExp(r'^(\d{4})-(\d{1,2})-(\d{1,2}) (\d{1,2}):(\d{1,2}):(\d{1,2})$');

DateTime? parseCapturedAtString(String? raw) {
  final m = _capturedAtStrRe.firstMatch((raw ?? '').trim());
  if (m == null) return null;
  final year = int.parse(m.group(1)!);
  final month = int.parse(m.group(2)!);
  final day = int.parse(m.group(3)!);
  final hour = int.parse(m.group(4)!);
  final minute = int.parse(m.group(5)!);
  final second = int.parse(m.group(6)!);
  final parsed = DateTime(year, month, day, hour, minute, second);
  // strptime parity: reject out-of-range components (month 13, Feb 31) that
  // Dart's DateTime would silently roll over.
  final valid = parsed.year == year &&
      parsed.month == month &&
      parsed.day == day &&
      parsed.hour == hour &&
      parsed.minute == minute &&
      parsed.second == second;
  return valid ? parsed : null;
}

/// Full `_parse_captured_at` parity: strict string parse + validation window.
DateTime? parseCapturedAt(String? raw,
        {required DateTime now,
        int maxAgeDays = SharedConstants.capturedAtMaxAgeDaysDefault}) =>
    validateCapturedAt(parseCapturedAtString(raw),
        now: now, maxAgeDays: maxAgeDays);

/// Validation window (spec §6.3): reject values more than 1 hour in the
/// future of local [now] or older than [maxAgeDays] (CAPTURED_AT_MAX_AGE_DAYS
/// = 45, clamp 1–365). Comparisons are strict '>' / '<': exactly now+1h and
/// exactly now−maxAgeDays are ACCEPTED (both boundary tests pin inclusive).
DateTime? validateCapturedAt(DateTime? captured,
    {required DateTime now,
    int maxAgeDays = SharedConstants.capturedAtMaxAgeDaysDefault}) {
  if (captured == null) return null;
  final ageDays = maxAgeDays < 1 ? 1 : (maxAgeDays > 365 ? 365 : maxAgeDays);
  if (captured.isAfter(now.add(
      const Duration(seconds: SharedConstants.capturedAtMaxFutureSeconds)))) {
    return null;
  }
  if (captured.isBefore(now.subtract(Duration(days: ageDays)))) return null;
  return captured;
}

/// Spec §6.3 priority order: (1) filename timestamp, (2) the library asset's
/// own createDateTime — each under the same validation window — else null
/// (callers then date the meal at intake time, the §6.3 fallback).
DateTime? deriveCapturedAt(
    {required String fileName,
    DateTime? assetCreateDate,
    DateTime? now,
    int maxAgeDays = SharedConstants.capturedAtMaxAgeDaysDefault}) {
  final clock = now ?? DateTime.now();
  return validateCapturedAt(capturedAtFromFilename(fileName),
          now: clock, maxAgeDays: maxAgeDays) ??
      validateCapturedAt(assetCreateDate, now: clock, maxAgeDays: maxAgeDays);
}
