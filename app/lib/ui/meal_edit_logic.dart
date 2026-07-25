/// Pure logic behind the meal editor (no widgets, no I/O) so every rule is
/// unit-testable: field parsing/bounds, totals derived from items, and the
/// analysis-map merge that decides what actually gets persisted.
///
/// Editing rules that matter downstream:
///   - the merge PRESERVES unknown keys from the original analysis
///     (analyzed_by, notes, anything a future model adds): the editor knows
///     about calories and macros, and must not silently drop the rest.
///   - a saved edit is always `is_food: true`. Every read path filters on
///     is_food (format.isFoodMeal), so keeping a false/absent flag would
///     hide the meal the user just typed numbers into.
///   - blank means ZERO, not "unknown": a blank protein field on a meal the
///     user is explicitly curating should read 0 g in totals, and the
///     display helpers render 0 for falsy anyway.
library;

import '../core/coerce.dart';
import '../core/contracts.dart';
import 'format.dart' show isoDate;

/// UI bounds. Deliberately generous — the point is to reject typos and
/// hostile input (1e9 breaks the coercion layer's accumulation guarantees),
/// not to police unusual meals.
const num maxMealCalories = 20000;
const num maxMacroGrams = 2000;
const int maxDescriptionChars = 300;
const int maxItemsPerMeal = 40;

/// One parsed numeric field: [value] null means the field was blank.
class FieldParse {
  const FieldParse(this.value, this.error);
  final num? value;
  final String? error;

  bool get ok => error == null;
}

/// Parse a user-typed number: blank → null (caller treats as 0), negative /
/// non-numeric / over-max → an error message naming the field.
FieldParse parseNumberField(String raw,
    {required String label, required num max}) {
  final t = raw.trim();
  if (t.isEmpty) return const FieldParse(null, null);
  final v = num.tryParse(t);
  if (v == null) return FieldParse(null, '$label must be a number.');
  if (v.isNaN || v.isInfinite) return FieldParse(null, '$label must be a number.');
  if (v < 0) return FieldParse(null, '$label cannot be negative.');
  if (v > max) return FieldParse(null, '$label looks too large (max $max).');
  return FieldParse(v, null);
}

/// A food item row in the editor.
class MealItemDraft {
  MealItemDraft({
    this.name = '',
    this.calories = '',
    this.protein = '',
    this.carbs = '',
    this.fat = '',
  });

  String name;
  String calories;
  String protein;
  String carbs;
  String fat;

  static MealItemDraft fromMap(Map<String, dynamic> m) => MealItemDraft(
        name: (m['name'] ?? '').toString(),
        calories: _numText(m['estimated_calories']),
        protein: _numText(m['protein_g']),
        carbs: _numText(m['carbs_g']),
        fat: _numText(m['fat_g']),
      );

  bool get isBlank =>
      name.trim().isEmpty &&
      calories.trim().isEmpty &&
      protein.trim().isEmpty &&
      carbs.trim().isEmpty &&
      fat.trim().isEmpty;

  Map<String, dynamic> toMap() => {
        'name': name.trim(),
        'estimated_calories': parseNumberField(calories,
                    label: 'Item calories', max: maxMealCalories)
                .value ??
            0,
        'protein_g':
            parseNumberField(protein, label: 'Item protein', max: maxMacroGrams)
                    .value ??
                0,
        'carbs_g':
            parseNumberField(carbs, label: 'Item carbs', max: maxMacroGrams)
                    .value ??
                0,
        'fat_g': parseNumberField(fat, label: 'Item fat', max: maxMacroGrams)
                .value ??
            0,
      };
}

String _numText(dynamic v) {
  final n = safeNumberOr(v, null);
  if (n == null) return '';
  return n == n.roundToDouble() ? n.round().toString() : n.toString();
}

/// The editable state of one meal (existing or new).
class MealDraft {
  MealDraft({
    required this.description,
    required this.dateIso,
    required this.time,
    required this.calories,
    required this.protein,
    required this.carbs,
    required this.fat,
    required this.items,
    this.base = const {},
  });

  String description;
  String dateIso; // YYYY-MM-DD
  String time; // hh:mm AM/PM, server display parity
  String calories;
  String protein;
  String carbs;
  String fat;
  List<MealItemDraft> items;

  /// The analysis map this draft started from; unknown keys survive the save.
  final Map<String, dynamic> base;

  static MealDraft fromMeal(Meal meal) => MealDraft(
        description: mealDescriptionRaw(meal.analysis),
        dateIso: meal.date,
        time: meal.time,
        calories: _numText(meal.analysis['total_calories']),
        protein: _numText(meal.analysis['total_protein_g']),
        carbs: _numText(meal.analysis['total_carbs_g']),
        fat: _numText(meal.analysis['total_fat_g']),
        items: [
          for (final m in safeFoodItems(meal.analysis)) MealItemDraft.fromMap(m)
        ],
        base: Map<String, dynamic>.from(meal.analysis),
      );

  static MealDraft blank(DateTime now) => MealDraft(
        description: '',
        dateIso: isoDate(now),
        time: formatClock(now),
        calories: '',
        protein: '',
        carbs: '',
        fat: '',
        items: [],
      );

  /// Sum of the item rows, for the "use item totals" action.
  ({num calories, num protein, num carbs, num fat}) itemTotals() {
    num c = 0, p = 0, cb = 0, f = 0;
    for (final it in items.where((i) => !i.isBlank)) {
      final m = it.toMap();
      c += safeNumber(m['estimated_calories']);
      p += safeNumber(m['protein_g']);
      cb += safeNumber(m['carbs_g']);
      f += safeNumber(m['fat_g']);
    }
    return (calories: c, protein: p, carbs: cb, fat: f);
  }

  /// Every problem the user must fix before saving, in field order.
  List<String> validate() {
    final errors = <String>[];
    if (description.trim().length > maxDescriptionChars) {
      errors.add('Description is too long (max $maxDescriptionChars).');
    }
    if (!isRealIsoDate(dateIso)) {
      errors.add('Date must be a real YYYY-MM-DD date.');
    }
    if (parseClock(time) == null) {
      errors.add('Time must look like 07:30 PM.');
    }
    for (final f in [
      (calories, 'Calories', maxMealCalories),
      (protein, 'Protein', maxMacroGrams),
      (carbs, 'Carbs', maxMacroGrams),
      (fat, 'Fat', maxMacroGrams),
    ]) {
      final p = parseNumberField(f.$1, label: f.$2, max: f.$3);
      if (!p.ok) errors.add(p.error!);
    }
    final rows = items.where((i) => !i.isBlank).toList();
    if (rows.length > maxItemsPerMeal) {
      errors.add('Too many items (max $maxItemsPerMeal).');
    }
    for (var i = 0; i < rows.length; i++) {
      final it = rows[i];
      final n = i + 1;
      for (final f in [
        (it.calories, 'Item $n calories', maxMealCalories),
        (it.protein, 'Item $n protein', maxMacroGrams),
        (it.carbs, 'Item $n carbs', maxMacroGrams),
        (it.fat, 'Item $n fat', maxMacroGrams),
      ]) {
        final p = parseNumberField(f.$1, label: f.$2, max: f.$3);
        if (!p.ok) errors.add(p.error!);
      }
    }
    return errors;
  }

  /// The analysis map to persist. Call only when [validate] is empty.
  Map<String, dynamic> toAnalysis() {
    final out = Map<String, dynamic>.from(base);
    num field(String raw, num max) =>
        parseNumberField(raw, label: 'x', max: max).value ?? 0;
    final desc = description.trim();
    out['meal_description'] = desc.isEmpty ? 'Meal' : desc;
    out['total_calories'] = field(calories, maxMealCalories);
    out['total_protein_g'] = field(protein, maxMacroGrams);
    out['total_carbs_g'] = field(carbs, maxMacroGrams);
    out['total_fat_g'] = field(fat, maxMacroGrams);
    // A hand-curated meal is food by definition — see the library note.
    out['is_food'] = true;
    out['food_items'] = [
      for (final it in items.where((i) => !i.isBlank)) it.toMap()
    ];
    return out;
  }
}

/// Description without the display fallback (the editor shows an empty field
/// rather than the literal word "Meal" for an unset description).
String mealDescriptionRaw(Map<String, dynamic> analysis) {
  final d = analysis['meal_description'];
  return d is String ? d.trim() : '';
}

/// True only for a calendar date that actually exists. DateTime.parse is NOT
/// enough: Dart ROLLS OVER out-of-range components ('2026-13-40' parses to
/// 2027-02-09, '2026-02-30' to March 2), which would silently file a meal on
/// a day the user never picked.
bool isRealIsoDate(String raw) {
  final m = RegExp(r'^(\d{4})-(\d{2})-(\d{2})$').firstMatch(raw.trim());
  if (m == null) return false;
  final y = int.parse(m.group(1)!);
  final mo = int.parse(m.group(2)!);
  final d = int.parse(m.group(3)!);
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return false;
  final dt = DateTime(y, mo, d);
  return dt.year == y && dt.month == mo && dt.day == d;
}

/// 12-hour zero-padded clock, matching the stored/server format.
String formatClock(DateTime t) {
  final h = t.hour % 12 == 0 ? 12 : t.hour % 12;
  final m = t.minute.toString().padLeft(2, '0');
  return '${h.toString().padLeft(2, '0')}:$m ${t.hour < 12 ? 'AM' : 'PM'}';
}

/// Parse the stored 12-hour clock back to (hour, minute); null when unusable.
({int hour, int minute})? parseClock(String raw) {
  final m = RegExp(r'^\s*(\d{1,2}):(\d{2})\s*([AaPp])\.?[Mm]\.?\s*$')
      .firstMatch(raw);
  if (m == null) return null;
  var h = int.parse(m.group(1)!);
  final min = int.parse(m.group(2)!);
  if (h < 1 || h > 12 || min > 59) return null;
  final pm = m.group(3)!.toLowerCase() == 'p';
  if (h == 12) h = 0;
  return (hour: pm ? h + 12 : h, minute: min);
}
