/// Report builders — plain-text ports of the server's report formatters.
///
/// Every formula and format string below is pinned by docs/APP_PORT_SPEC.md
/// §5 (which cites telegram_bot.py / daily_report.py / database.py). The UI
/// renders these strings monospace; the server's Telegram-HTML tags are
/// dropped but content, emoji, and number formatting match.
library;

import 'dart:convert';
import 'dart:math' as math;

import '../../core/coerce.dart';
import '../../core/contracts.dart';

/// Factory the integrator wires (INTEGRATION CONTRACT).
ReportBuilder createReportBuilder(MealsDao dao) => ReportBuilderImpl(dao);

// ─── Coercions the spec pins beyond core/coerce.dart ───────────────
// (core/ is frozen; these helpers live here and are exported for tests.)

/// Python truthiness over every JSON shape. Spec §3.5: is_food filtering is
/// `analysis.get("is_food")` TRUTHY, never `== true` (database.py:453-461
/// spells the same table out in SQL). false/0/""/[]/{}/null → false.
bool isFoodTruthy(dynamic value) {
  if (value == null) return false;
  if (value is bool) return value;
  if (value is num) return value != 0; // NaN != 0 → truthy, like Python
  if (value is String) return value.isNotEmpty;
  if (value is List) return value.isNotEmpty;
  if (value is Map) return value.isNotEmpty;
  return true;
}

/// utils._as_number port (spec §3.5): null — not a default — for non-numeric,
/// bool, NaN/Infinity, and |x| >= 1e9 values, so callers can distinguish
/// "junk" from a real 0.
num? boundedNumber(dynamic value) {
  if (value is bool || value is! num) return null;
  final d = value.toDouble();
  if (d.isNaN || d.isInfinite) return null;
  if (d <= -1e9 || d >= 1e9) return null;
  return value;
}

/// utils.meal_calorie_mismatch port (spec §3.5): the item-calorie sum when it
/// contradicts the stored total beyond max(100 kcal, 20%), else null. No
/// counted items, a non-positive item sum, or an unusable total all read as
/// "consistent" — a crashed warning would be worse than the bug it flags.
int? mealCalorieMismatch(Map<String, dynamic> analysis) {
  num itemSum = 0;
  var counted = 0;
  for (final item in safeFoodItems(analysis)) {
    final cal = boundedNumber(item['estimated_calories']);
    if (cal != null) {
      itemSum += cal;
      counted++;
    }
  }
  final total = boundedNumber(analysis['total_calories']);
  if (counted == 0 || itemSum <= 0 || total == null) return null;
  if ((total - itemSum).abs() > math.max(100, 0.2 * math.max(total, itemSum))) {
    return itemSum.truncate(); // Python int(): truncation toward zero
  }
  return null;
}

/// Python round(): banker's rounding (half-to-even), returning int — Dart's
/// .round() is half-away-from-zero and would drift on exact .5 ties.
int pyRound(num value) {
  final floor = value.floorToDouble();
  final diff = value - floor;
  final f = floor.toInt();
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f.isEven ? f : f + 1;
}

/// Python `{:,}`: thousands separators on the integral digits; a float's
/// fractional part rides through unchanged (spec §5 formatting preamble).
String commaFmt(num value) {
  var s = value.toString();
  var sign = '';
  if (s.startsWith('-')) {
    sign = '-';
    s = s.substring(1);
  }
  // Exponent/NaN/Infinity renderings can't meaningfully group; upstream
  // safeNumber clamps make them unreachable on spec'd paths.
  if (s.contains('e') || s.contains('E') || s.contains('N') || s.contains('I')) {
    return sign + s;
  }
  final dot = s.indexOf('.');
  final intPart = dot == -1 ? s : s.substring(0, dot);
  final rest = dot == -1 ? '' : s.substring(dot);
  final grouped = StringBuffer();
  for (var i = 0; i < intPart.length; i++) {
    if (i > 0 && (intPart.length - i) % 3 == 0) grouped.write(',');
    grouped.write(intPart[i]);
  }
  return '$sign$grouped$rest';
}

/// Python `{:+,}`: always-signed thousands-separated.
String signedCommaFmt(num value) =>
    value < 0 ? commaFmt(value) : '+${commaFmt(value)}';

/// Python `%g` (spec §5.1 activity distance): up to 6 significant digits,
/// trailing zeros trimmed, integral values without the ".0".
String gFmt(num value) {
  final d = value.toDouble();
  if (d == 0) return '0';
  var s = d.toStringAsPrecision(6);
  if (!s.contains('e') && s.contains('.')) {
    s = s.replaceFirst(RegExp(r'0+$'), '');
    s = s.replaceFirst(RegExp(r'\.$'), '');
  }
  return s;
}

/// statistics.median port (spec §5.1 typical-day rule): sort; odd count →
/// middle element; even count → mean of the two middle elements.
num pyMedian(List<num> values) {
  final sorted = [...values]..sort();
  final n = sorted.length;
  if (n.isOdd) return sorted[n ~/ 2];
  return (sorted[n ~/ 2 - 1] + sorted[n ~/ 2]) / 2;
}

/// daily_report._weekly_weight_slope port (spec §5.5): least-squares kg/week
/// slope (per-day OLS × 7, rounded to 2 decimals), or null when fewer than 2
/// usable weigh-ins. Junk weights/dates are skipped, never thrown on.
double? weeklyWeightSlope(List<Map<String, dynamic>> weights) {
  final points = <(int, num)>[];
  for (final row in weights) {
    final dateStr = '${row['date']}';
    final day =
        dateStr.length >= 10 ? _parseIsoDay(dateStr.substring(0, 10)) : null;
    if (day == null) continue;
    final kg = safeNumber(row['weight_kg']);
    if (kg > 0) {
      points.add(
          (day.millisecondsSinceEpoch ~/ Duration.millisecondsPerDay, kg));
    }
  }
  if (points.length < 2) return null;
  final n = points.length;
  final meanX = points.fold<num>(0, (s, p) => s + p.$1) / n;
  final meanY = points.fold<num>(0, (s, p) => s + p.$2) / n;
  num numerator = 0;
  num denominator = 0;
  for (final (x, y) in points) {
    numerator += (x - meanX) * (y - meanY);
    denominator += (x - meanX) * (x - meanX);
  }
  if (denominator == 0) return null;
  return pyRound(numerator / denominator * 7 * 100) / 100; // round(x, 2)
}

// ─── Date plumbing (English strftime names; spec pins server output) ─

const List<String> _weekdays = [
  'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday',
];
const List<String> _monthsLong = [
  'January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December',
];
const List<String> _monthsAbbr = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

final RegExp _isoDayRe = RegExp(r'^\d{4}-\d{2}-\d{2}$');

String _two(int n) => n.toString().padLeft(2, '0');

String _isoDay(DateTime d) =>
    '${d.year.toString().padLeft(4, '0')}-${_two(d.month)}-${_two(d.day)}';

/// Local date → UTC midnight so day arithmetic never crosses a DST edge.
DateTime _utcDay(DateTime d) => DateTime.utc(d.year, d.month, d.day);

/// Strict YYYY-MM-DD parse (Python date.fromisoformat parity): rejects
/// normalized overflow like 2026-02-30 by round-tripping the components.
DateTime? _parseIsoDay(String s) {
  if (!_isoDayRe.hasMatch(s)) return null;
  final d = DateTime.tryParse('${s}T00:00:00Z');
  if (d == null || _isoDay(d) != s) return null;
  return d;
}

// ─── The builder ───────────────────────────────────────────────────

class ReportBuilderImpl implements ReportBuilder {
  ReportBuilderImpl(this._dao, {DateTime Function()? clock})
      : _clock = clock ?? DateTime.now;

  final MealsDao _dao;

  /// Injectable clock: spec §2.2 app simplification — one clock, the device's.
  final DateTime Function() _clock;

  Future<List<Meal>> _foodMeals(String start, String end) async =>
      (await _dao.mealsBetween(start, end))
          .where((m) => isFoodTruthy(m.analysis['is_food']))
          .toList();

  /// telegram_bot._daily_calorie_totals (spec §5.1/§5.3): per-day sums over
  /// food meals, each meal clamped max(0, safeNumber) — a negative
  /// hallucinated total must never subtract from a day.
  Future<Map<String, num>> _dailyCalorieTotals(String start, String end) async {
    final totals = <String, num>{};
    for (final m in await _foodMeals(start, end)) {
      final cal = safeNumber(m.analysis['total_calories']);
      totals[m.date] = (totals[m.date] ?? 0) + (cal > 0 ? cal : 0);
    }
    return totals;
  }

  /// Weight/activity rows are not first-class DAO reads; they ride in the
  /// exportJson payload (spec §8 full export). Unexpected shapes degrade to
  /// "no data" — every consumer section is optional by spec.
  Future<Map<String, dynamic>> _exportTables() async {
    try {
      final decoded = jsonDecode(await _dao.exportJson());
      if (decoded is Map) return decoded.cast<String, dynamic>();
    } catch (_) {
      // Fall through: reports render without fitness sections.
    }
    return const {};
  }

  List<Map<String, dynamic>> _tableRows(
      Map<String, dynamic> export, List<String> keys) {
    for (final key in keys) {
      final rows = export[key];
      if (rows is List) {
        return rows
            .whereType<Map>()
            .map((r) => r.cast<String, dynamic>())
            .toList();
      }
    }
    return const [];
  }

  List<Map<String, dynamic>> _rowsBetween(List<Map<String, dynamic>> rows,
          String start, String end) =>
      rows.where((r) {
        final d = r['date'];
        return d is String && d.compareTo(start) >= 0 && d.compareTo(end) <= 0;
      }).toList()
        ..sort((a, b) => '${a['date']}'.compareTo('${b['date']}'));

  List<Map<String, dynamic>> _activitiesBetween(
          Map<String, dynamic> export, String start, String end) =>
      _rowsBetween(_tableRows(export, const ['activities']), start, end);

  List<Map<String, dynamic>> _weightsBetween(
          Map<String, dynamic> export, String start, String end) =>
      _rowsBetween(
          _tableRows(export,
              const ['body_weight', 'body_weights', 'bodyWeight', 'weights']),
          start,
          end);

  Map<String, dynamic>? _decodedRaw(dynamic raw) {
    if (raw is Map) return raw.cast<String, dynamic>();
    if (raw is String) {
      // The activities.raw column is JSON text (spec §2.1).
      try {
        final decoded = jsonDecode(raw);
        if (decoded is Map) return decoded.cast<String, dynamic>();
      } catch (_) {
        // Non-JSON raw blob: contributes no steps.
      }
    }
    return null;
  }

  /// telegram_bot._activity_totals (spec §5.1): junk-tolerant (burned, steps,
  /// km) sums; steps ride inside the raw JSON blob and only count when raw is
  /// a map.
  ({num burned, num steps, num km}) _activityTotals(
      List<Map<String, dynamic>> rows) {
    num burned = 0;
    num steps = 0;
    num km = 0;
    for (final row in rows) {
      burned += safeNumber(row['active_calories']);
      final raw = _decodedRaw(row['raw']);
      if (raw != null) steps += safeNumber(raw['steps']);
      km += safeNumber(row['distance_km']);
    }
    return (burned: burned, steps: steps, km: km);
  }

  String _mealDescription(Map<String, dynamic> a) =>
      a.containsKey('meal_description') ? '${a['meal_description']}' : 'Unknown';

  @override
  Future<String> todaySummary() async {
    final now = _clock();
    final todayUtc = _utcDay(now);
    final today = _isoDay(todayUtc);
    final meals = await _foodMeals(today, today);
    if (meals.isEmpty) {
      return '📋 Today\'s Summary\n\nNo meals logged yet today.';
    }

    num totalCal = 0;
    num totalP = 0;
    num totalC = 0;
    num totalF = 0;
    final lines = <String>['📋 Today\'s Summary', ''];

    // Per-meal lines in the /meals list format (spec §5.2); the totals below
    // accumulate the same safeNumber values with NO negative clamp (§5.1).
    for (var i = 0; i < meals.length; i++) {
      final meal = meals[i];
      final a = meal.analysis;
      final cal = safeNumber(a['total_calories']);
      final p = safeNumber(a['total_protein_g']);
      final c = safeNumber(a['total_carbs_g']);
      final f = safeNumber(a['total_fat_g']);
      totalCal += cal;
      totalP += p;
      totalC += c;
      totalF += f;
      final corrected = meal.corrected ? ' ✏️' : '';
      lines.add('${i + 1}. ${_mealDescription(a)} (${meal.time})$corrected');
      lines.add('   ~$cal kcal | P:${p}g C:${c}g F:${f}g');
    }

    lines
      ..add('')
      ..add('🔥 ${commaFmt(totalCal)} kcal')
      ..add('🥩 Protein: ${totalP}g')
      ..add('🍞 Carbs: ${totalC}g')
      ..add('🧈 Fat: ${totalF}g')
      ..add('📸 Meals: ${meals.length}');

    // Net line only with a real burn today; a distance/steps-only session
    // still gets an activity line instead of vanishing (spec §5.1).
    final export = await _exportTables();
    final t = _activityTotals(_activitiesBetween(export, today, today));
    if (t.burned > 0) {
      lines.add('🔥 Burned: ${commaFmt(pyRound(t.burned))} kcal');
      lines.add('⚖️ Net: ${commaFmt(pyRound(totalCal - t.burned))} kcal');
    } else if (t.steps != 0 || t.km != 0) {
      final bits = <String>[
        if (t.km != 0) '${gFmt(t.km)} km',
        if (t.steps != 0) '${commaFmt(pyRound(t.steps))} steps',
      ];
      lines.add('🏃 Activity: ${bits.join(' · ')}');
    }

    // Typical-day: MEDIAN over the prior 7 local days, today excluded, only
    // when ≥ 2 days have data — under-logged days would bias a mean low
    // (spec §5.1).
    final prior = await _dailyCalorieTotals(
      _isoDay(todayUtc.subtract(const Duration(days: 7))),
      _isoDay(todayUtc.subtract(const Duration(days: 1))),
    );
    if (prior.length >= 2) {
      final typical = pyMedian(prior.values.toList()).truncate();
      lines
        ..add('')
        ..add('📊 Typical day: ~${commaFmt(typical)} kcal');
      if (totalCal <= typical) {
        lines.add(
            '⏳ ~${commaFmt(typical - totalCal)} kcal headroom vs typical');
      } else {
        lines.add('📈 ~${commaFmt(totalCal - typical)} kcal above typical');
      }
    }
    return lines.join('\n');
  }

  @override
  Future<String> history({int days = 30}) async {
    // Inclusive window [today - (days-1), today] (spec §5.3).
    final todayUtc = _utcDay(_clock());
    final today = _isoDay(todayUtc);
    final start = _isoDay(todayUtc.subtract(Duration(days: days - 1)));
    final totals = await _dailyCalorieTotals(start, today);
    if (totals.isEmpty) {
      return '📅 $days-Day History\n\nNo meals logged in the past $days days.';
    }

    final lines = <String>['📅 $days-Day History', ''];
    final dates = totals.keys.toList()..sort((a, b) => b.compareTo(a));
    for (final d in dates) {
      final parsed = _parseIsoDay(d);
      final String friendly;
      if (parsed == null) {
        friendly = d; // an unparseable stored date renders raw (spec §5.3)
      } else if (d == today) {
        friendly = 'Today';
      } else {
        friendly = '${_weekdays[parsed.weekday - 1]}, '
            '${_monthsAbbr[parsed.month - 1]} ${_two(parsed.day)}';
      }
      lines.add('• $friendly: ~${totals[d]} kcal');
    }

    // Averaged over days that HAVE data, not the whole window; int() truncates.
    final avg = totals.values.reduce((a, b) => a + b) / totals.length;
    lines
      ..add('')
      ..add('📊 Average: ~${avg.truncate()} kcal / day');
    return lines.join('\n');
  }

  @override
  Future<String> dailyReport(String date) async {
    final target = _parseIsoDay(date);
    if (target == null) {
      throw ArgumentError.value(date, 'date', 'must be YYYY-MM-DD');
    }

    final lines = <String>[
      '📊 Daily Calorie Report',
      '📅 ${_weekdays[target.weekday - 1]}, '
          '${_monthsLong[target.month - 1]} ${_two(target.day)}, ${target.year}',
      '',
    ];

    final allMeals = await _dao.mealsBetween(date, date);
    final foodMeals =
        allMeals.where((m) => isFoodTruthy(m.analysis['is_food'])).toList();
    final export = await _exportTables();

    if (foodMeals.isEmpty) {
      // Spec §5.5: empty-day message plus any fitness sections, then stop.
      lines.add('No meals logged today. 🍽️');
      lines.addAll(_weightSection(export, target, date));
      return lines.join('\n');
    }

    lines
      ..add('🍽️ Meals:')
      ..add('');

    num grandCal = 0;
    num grandP = 0;
    num grandC = 0;
    num grandF = 0;
    final seenMealKeys = <String, int>{};

    for (var i = 0; i < foodMeals.length; i++) {
      final meal = foodMeals[i];
      final a = meal.analysis;
      final index = i + 1;
      final cal = safeNumber(a['total_calories']);
      final p = safeNumber(a['total_protein_g']);
      final c = safeNumber(a['total_carbs_g']);
      final f = safeNumber(a['total_fat_g']);
      grandCal += cal;
      grandP += p;
      grandC += c;
      grandF += f;

      final corrected = meal.corrected ? ' ✏️' : '';
      lines.add('$index. ${_mealDescription(a)} — ${meal.time}$corrected');

      for (final item in safeFoodItems(a)) {
        final name = item.containsKey('name') ? '${item['name']}' : '?';
        final itemCal = safeNumber(item['estimated_calories']);
        final itemP = safeNumber(item['protein_g']);
        final itemC = safeNumber(item['carbs_g']);
        final itemF = safeNumber(item['fat_g']);
        lines
          ..add('  • $name: $itemCal kcal')
          ..add('    P:${itemP}g | C:${itemC}g | F:${itemF}g');
      }
      lines.add('  📊 Subtotal: ~$cal kcal | P:${p}g C:${c}g F:${f}g');

      // Contradicting-totals flag — suppressed on corrected meals: the user
      // already vouched for the numbers (spec §5.5, daily_report.py:421).
      final itemSum = mealCalorieMismatch(a);
      if (itemSum != null && !meal.corrected) {
        lines.add('  ⚠️ Item calories sum to ~$itemSum kcal but the meal '
            'total is ~$cal kcal — this entry may be wrong.');
      }

      // Likely-duplicate flag (display-only, NO writes): hash key when the
      // meal has one, else the (time, description, total) tuple for
      // hash-less paths like manual text meals (spec §5.5).
      final key = meal.imageHash.isNotEmpty
          ? 'h:${meal.imageHash}'
          : 't:${meal.time}|${a['meal_description']}|${a['total_calories']}';
      final firstIndex = seenMealKeys[key];
      if (firstIndex != null) {
        lines.add('  ⚠️ Possible duplicate of meal $firstIndex.');
      } else {
        seenMealKeys[key] = index;
      }
      lines.add('');
    }

    lines
      ..add('━━━━━━━━━━━━━━━━━━')
      ..add('📊 Daily Summary')
      ..add('')
      ..add('🔥 Total Calories: ~${commaFmt(grandCal)} kcal')
      ..add('🥩 Protein: ${grandP}g')
      ..add('🍞 Carbs: ${grandC}g')
      ..add('🧈 Fat: ${grandF}g')
      ..add('📸 Meals logged: ${foodMeals.length}');

    // 7-day average over the prior window [date-7, date-1]. A meal only
    // counts when its RAW total is a non-bool number with 0 < cal < 1e9 —
    // strictly positive, and deliberately a bounds test rather than
    // isFinite (spec §5.5, daily_report.py:450-470).
    final priorTotals = <String, num>{};
    final priorMeals = await _dao.mealsBetween(
      _isoDay(target.subtract(const Duration(days: 7))),
      _isoDay(target.subtract(const Duration(days: 1))),
    );
    for (final m in priorMeals) {
      if (!isFoodTruthy(m.analysis['is_food'])) continue;
      final cal = m.analysis['total_calories'];
      if (cal is! num) continue;
      final d = cal.toDouble();
      if (!(d > 0 && d < 1e9)) continue; // NaN/Infinity fail these tests too
      priorTotals[m.date] = (priorTotals[m.date] ?? 0) + cal;
    }
    if (priorTotals.length >= 2) {
      final avg =
          pyRound(priorTotals.values.reduce((a, b) => a + b) / priorTotals.length);
      lines.add('📈 7-day avg: ~${commaFmt(avg)} kcal');
      final delta = grandCal - avg;
      var deltaLine = '    Today vs avg: ${signedCommaFmt(delta)} kcal';
      if (avg != 0) {
        deltaLine += ' (${_signedPct(delta / avg * 100)}%)';
      }
      lines.add(deltaLine);
    }

    // Macro split: kcal-weighted 4/4/9 percentages, only when the macro
    // calories are positive (spec §5.5).
    final totalMacroCal = grandP * 4 + grandC * 4 + grandF * 9;
    if (totalMacroCal > 0) {
      lines
        ..add('')
        ..add('Macro Split:')
        ..add('  🥩 Protein: ${pyRound(grandP * 4 / totalMacroCal * 100)}%')
        ..add('  🍞 Carbs: ${pyRound(grandC * 4 / totalMacroCal * 100)}%')
        ..add('  🧈 Fat: ${pyRound(grandF * 9 / totalMacroCal * 100)}%');
    }

    lines.addAll(_weightSection(export, target, date));
    return lines.join('\n');
  }

  /// Python's f"{x:+.0f}": always-signed half-even integer rendering; small
  /// negatives keep the "-0" Python emits.
  String _signedPct(double pct) {
    final sign = pct < 0 ? '-' : '+';
    return '$sign${pyRound(pct.abs())}';
  }

  /// daily_report._section_weight (spec §5.5): only when weigh-ins exist in
  /// the trailing 7-day window. "Latest" anchors on the newest weigh-in
  /// at/before the report date (366-day lookback) so a catch-up report for
  /// yesterday never borrows this morning's weigh-in.
  List<String> _weightSection(
      Map<String, dynamic> export, DateTime target, String date) {
    final windowStart = _isoDay(target.subtract(const Duration(days: 7)));
    final weights = _weightsBetween(export, windowStart, date);
    if (weights.isEmpty) return const [];

    final lines = <String>['', '⚖️ Weight'];
    final anchorFloor = _isoDay(target.subtract(const Duration(days: 366)));
    final anchors = _weightsBetween(export, anchorFloor, date);
    final num latestKg =
        anchors.isEmpty ? 0 : safeNumber(anchors.last['weight_kg']);
    if (latestKg > 0) {
      lines.add('Latest: ${latestKg.toStringAsFixed(1)} kg');
    }
    final slope = weeklyWeightSlope(weights);
    if (slope != null) {
      final arrow = slope > 0 ? '⬆️' : (slope < 0 ? '⬇️' : '➡️');
      final signed = slope < 0
          ? slope.toStringAsFixed(2)
          : '+${slope.toStringAsFixed(2)}';
      lines.add('7-day trend: $arrow $signed kg/wk');
    }
    return lines;
  }

  @override
  Future<String> stats() async {
    final todayUtc = _utcDay(_clock());
    final today = _isoDay(todayUtc);
    final sevenDaysAgo = _isoDay(todayUtc.subtract(const Duration(days: 6)));

    // All-time window [1970-01-01, today], mirroring get_meal_stats
    // semantics (spec §5.6): Python-truthy is_food, safeNumber calorie
    // clamps, distinct non-empty dates, ''-source → 'unknown'.
    final all = await _dao.mealsBetween('1970-01-01', today);
    final food =
        all.where((m) => isFoodTruthy(m.analysis['is_food'])).toList();

    final todayCount = food.where((m) => m.date == today).length;
    final foodRecent =
        food.where((m) => m.date.compareTo(sevenDaysAgo) >= 0).toList();
    num recentCalories = 0;
    for (final m in foodRecent) {
      recentCalories += safeNumber(m.analysis['total_calories']);
    }

    num totalCalories = 0;
    final activeDays = <String>{};
    final sourceCounts = <String, int>{};
    for (final m in food) {
      totalCalories += safeNumber(m.analysis['total_calories']);
      if (m.date.isNotEmpty) activeDays.add(m.date);
      final source = m.source.isEmpty ? 'unknown' : m.source;
      sourceCounts[source] = (sourceCounts[source] ?? 0) + 1;
    }

    final lines = <String>[
      '📈 Database Stats',
      'Food meals today: $todayCount',
      'Food meals last 7 days: ${foodRecent.length}',
      'Food meals all time: ${food.length}',
      'Raw DB rows all time: ${all.length}',
      'Calories last 7 days: ~${commaFmt(recentCalories.truncate())}',
      'Calories all time: ~${commaFmt(totalCalories.truncate())}',
    ];
    if (activeDays.isNotEmpty) {
      final avg = (totalCalories / activeDays.length).truncate();
      lines.add('Average per active day: ~${commaFmt(avg)} kcal');
    }
    if (sourceCounts.isNotEmpty) {
      lines
        ..add('')
        ..add('Sources');
      final entries = sourceCounts.entries.toList()
        ..sort((a, b) => a.value != b.value
            ? b.value.compareTo(a.value)
            : a.key.compareTo(b.key)); // count desc, then name (spec §5.6)
      for (final e in entries) {
        lines.add('• ${e.key}: ${e.value}');
      }
    }
    return lines.join('\n');
  }
}
