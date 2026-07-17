/// Render-side coercion and the shared view formulas (spec §3.5 display
/// fallbacks; §5.1 today totals/typical-day; §5.3 history grouping).
///
/// Stored analyses are untrusted (spec §1.2, §3.5): every read here degrades
/// hostile shapes instead of throwing, so a poison row can never crash a
/// screen.
library;

import 'package:intl/intl.dart';

import '../core/coerce.dart';
import '../core/contracts.dart';

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

String isoDate(DateTime d) => DateFormat('yyyy-MM-dd').format(d);

/// History day label: 'Today' for today, else `%A, %b %d` e.g.
/// 'Tuesday, Jul 15' (spec §5.3).
String friendlyHistoryDay(String date, {DateTime? now}) {
  final today = isoDate(now ?? DateTime.now());
  if (date == today) return 'Today';
  final parsed = DateTime.tryParse(date);
  if (parsed == null) return date;
  return DateFormat('EEEE, MMM dd').format(parsed);
}
