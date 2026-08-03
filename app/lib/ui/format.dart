/// Render-side coercion and the shared view formulas (spec §3.5 display
/// fallbacks; §5.1 today totals/typical-day; §5.3 history grouping).
///
/// Stored analyses are untrusted (spec §1.2, §3.5): every read here degrades
/// hostile shapes instead of throwing, so a poison row can never crash a
/// screen.
library;

import 'package:intl/intl.dart';

import '../core/coerce.dart';
import '../data/meals_logic.dart' as logic;
import '../core/contracts.dart';
import 'meal_edit_logic.dart' show parseClock;

final NumberFormat _thousands = NumberFormat('#,##0');

/// Thousands-separated integer calories (spec §5 formatting rules).
String formatKcal(num v) => _thousands.format(v.round());

/// Python truthiness for is_food (spec §3.5): false, 0, 0.0, '', [], {},
/// null → false; every other value → true. Food filtering uses THIS, never
/// `== true`.
bool isFoodTruthy(dynamic v) {
  if (v == null || v == false) return false;
  if (v == true) return true;
  if (v is num) return v != 0;
  if (v is String) return v.isNotEmpty;
  if (v is List) return v.isNotEmpty;
  if (v is Map) return v.isNotEmpty;
  return true;
}

bool isFoodMeal(Meal m) => isFoodTruthy(m.analysis['is_food']);

/// Meal description with a plain fallback (renderers never assume a string).
String mealDescription(Map<String, dynamic> analysis) {
  final d = analysis['meal_description'];
  if (d is String && d.trim().isNotEmpty) return d.trim();
  return 'Meal';
}

/// Card total calories: raw value `or "?"` (spec §3.5 display fallbacks —
/// falsy → '?', otherwise the raw stringified value).
String displayTotalCalories(Map<String, dynamic> analysis) {
  final v = analysis['total_calories'];
  return isFoodTruthy(v) ? '$v' : '?';
}

/// Card macro grams: raw value `or 0` (spec §3.5 display fallbacks).
String displayMacro(Map<String, dynamic> analysis, String key) {
  final v = analysis[key];
  return isFoodTruthy(v) ? '$v' : '0';
}

/// Meals in the order the user experienced them: by the DISPLAY clock, not
/// the ingestion timestamp the DAO sorts on. A meal moved to another day (or
/// added by hand later) otherwise lands wherever its row was created —
/// always last on the target day. Unparseable times keep their relative
/// order at the end.
List<Meal> byMealClock(Iterable<Meal> meals) {
  int minutes(Meal m) {
    final t = parseClock(m.time);
    return t == null ? 24 * 60 + 1 : t.hour * 60 + t.minute;
  }

  // Index-decorated so the sort is STABLE without pulling in a package:
  // equal (date, clock) pairs keep the DAO's own order.
  final decorated = meals.toList().asMap().entries.toList();
  decorated.sort((a, b) {
    final byDate = a.value.date.compareTo(b.value.date);
    if (byDate != 0) return byDate;
    final byClock = minutes(a.value).compareTo(minutes(b.value));
    return byClock != 0 ? byClock : a.key.compareTo(b.key);
  });
  return [for (final e in decorated) e.value];
}

/// Numeric macro grams for CHARTS (the display helpers above return
/// strings): coerced, negatives clamped to 0 so a hostile value can't
/// invert a stacked segment.
num safeMacro(Map<String, dynamic> analysis, String key) {
  final v = safeNumber(analysis[key]);
  return v < 0 ? 0 : v;
}

/// Item calories: safeNumber when numeric, raw string otherwise
/// (spec §3.5 / §5.4 item display fallback).
String displayItemCalories(dynamic v) {
  if (v is num && v is! bool) return '${safeNumber(v)}';
  return v == null ? '?' : '$v';
}

/// Today-header totals: Σ safeNumber over food meals, NO negative clamp on
/// this path (spec §5.1).
({num cal, num protein, num carbs, num fat, int meals}) todayTotals(
    List<Meal> meals) {
  num cal = 0, p = 0, c = 0, f = 0;
  var n = 0;
  for (final m in meals.where(isFoodMeal)) {
    n++;
    cal += safeNumber(m.analysis['total_calories']);
    p += safeNumber(m.analysis['total_protein_g']);
    c += safeNumber(m.analysis['total_carbs_g']);
    f += safeNumber(m.analysis['total_fat_g']);
  }
  return (cal: cal, protein: p, carbs: c, fat: f, meals: n);
}

/// Per-day calorie totals: food meals only, per-meal contribution clamped to
/// max(0, safeNumber(total_calories)), grouped by the stored date
/// (spec §5.1 typical-day / §5.3 history).
Map<String, num> dailyCalorieTotals(Iterable<Meal> meals) {
  final out = <String, num>{};
  for (final m in meals.where(isFoodMeal)) {
    final v = safeNumber(m.analysis['total_calories']);
    out[m.date] = (out[m.date] ?? 0) + (v > 0 ? v : 0);
  }
  return out;
}

/// Median of the per-day totals, only when >= 2 days have data — median, not
/// mean, so under-logged days don't bias low (spec §5.1). Display as int().
int? typicalDayKcal(Map<String, num> perDay) {
  if (perDay.length < 2) return null;
  final vals = perDay.values.map((v) => v.toDouble()).toList()..sort();
  final mid = vals.length ~/ 2;
  final median =
      vals.length.isOdd ? vals[mid] : (vals[mid - 1] + vals[mid]) / 2;
  return median.truncate(); // int(median) truncation, spec §5.1
}

/// Re-exported from the data layer on purpose: this value is WRITTEN to
/// meals.date and compared with string ranges in SQL, so it must be
/// Gregorian with ASCII digits regardless of the device locale.
/// `DateFormat('yyyy-MM-dd')` is locale-sensitive (Buddhist-era years,
/// Eastern-Arabic digits), which on the photo/editor write paths would
/// persist dates that no query could match.
String isoDate(DateTime d) => logic.isoDate(d);

/// History day label: [todayLabel] for today, else [pattern] via intl —
/// defaults reproduce the original English `%A, %b %d` e.g.
/// 'Tuesday, Jul 15' (spec §5.3). UI callers go through
/// `context.friendlyDay`, which feeds the locale's pattern and 今天/Today.
String friendlyHistoryDay(String date,
    {DateTime? now,
    String? localeName,
    String pattern = 'EEEE, MMM dd',
    String todayLabel = 'Today'}) {
  final today = isoDate(now ?? DateTime.now());
  if (date == today) return todayLabel;
  final parsed = DateTime.tryParse(date);
  if (parsed == null) return date;
  return DateFormat(pattern, localeName).format(parsed);
}

/// Stored meal clock → locale display. English passes the stored string
/// through UNTOUCHED (it already is the en display; intl's own en output
/// would sneak in a U+202F before AM). Chinese reformats to the 24-hour
/// '08:05' CLDR gives zh. STORAGE is untouched — meals.time keeps the
/// server-parity `%I:%M %p` shape; anything unparseable renders raw
/// (poison rows degrade, spec §1.2).
String displayClock(String raw, {String? localeName}) {
  if (localeName == null || !localeName.startsWith('zh')) return raw;
  final t = parseClock(raw);
  if (t == null) return raw;
  return DateFormat.jm(localeName)
      .format(DateTime(2000, 1, 1, t.hour, t.minute));
}
