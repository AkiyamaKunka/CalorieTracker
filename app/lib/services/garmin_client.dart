/// Garmin daily summary via the user's own server (spec §9, 2026-08-02).
///
/// The server holds the Garmin session token (garmin.py) and exposes one
/// pure read: POST /api/garmin_daily {date}. This client is deliberately
/// dumb — configured-or-null, available-or-null — because its ONE consumer
/// is a cosmetic line on Today that must never block or error the screen.
/// The server caches per date (10 min), so calling on every tab select is
/// fine.
library;

import 'dart:convert';

import 'package:http/http.dart' as http;

import '../core/coerce.dart' show safeNumber;
import 'settings/app_settings.dart';

/// One day's activity as Today consumes it.
class GarminDaily {
  const GarminDaily({
    required this.activeCalories,
    required this.steps,
    required this.distanceM,
    required this.activityCount,
  });
  final double activeCalories;
  final double steps;
  final double distanceM;
  final int activityCount;
}

typedef GarminDailyFetch = Future<GarminDaily?> Function(String date);

/// Build the fetch, or return null when the server is not configured —
/// callers treat null-fetch and null-result identically (no line drawn).
GarminDailyFetch makeGarminDailyFetch(AppSettings settings,
    {http.Client? client}) {
  final http.Client c = client ?? http.Client();
  return (String date) async {
    final base = settings.serverBaseUrl.trim();
    final key = settings.serverApiKey?.trim() ?? '';
    if (base.isEmpty || key.isEmpty) return null;
    try {
      final resp = await c
          .post(Uri.parse('$base/api/garmin_daily'),
              headers: {
                'Content-Type': 'application/json',
                'X-API-Key': key,
              },
              body: jsonEncode({'date': date}))
          .timeout(const Duration(seconds: 8));
      return parseGarminDailyBody(resp.statusCode, resp.body);
    } catch (_) {
      return null; // cosmetic feature: any failure means "no line today"
    }
  };
}

/// Pure response → summary mapping (exported for tests): anything that is
/// not a 200 with available:true is "no data", never an error.
GarminDaily? parseGarminDailyBody(int statusCode, String body) {
  if (statusCode != 200) return null;
  final Object? decoded;
  try {
    decoded = jsonDecode(body);
  } catch (_) {
    return null;
  }
  if (decoded is! Map || decoded['available'] != true) return null;
  // safeNumber shape: NaN/Infinity/absurd magnitudes → 0. Infinity passed
  // the Today screen's `> 0` guard and crashed formatKcal (pressure-test
  // find, 2026-08-03) — this parser exists to treat responses as
  // untrusted, so it must sanitize magnitude too.
  double num_(Object? v) => safeNumber(v).toDouble();
  return GarminDaily(
    activeCalories: num_(decoded['active_calories']),
    steps: num_(decoded['steps']),
    distanceM: num_(decoded['distance_m']),
    activityCount:
        decoded['activity_count'] is int ? decoded['activity_count'] as int : 0,
  );
}
