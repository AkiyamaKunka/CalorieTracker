/// Leftover deduction — the PURE half (user feature 2026-08-04).
///
/// The model only estimates FRACTIONS (what share of each item remains);
/// every number that lands in the database is computed here, clamped and
/// deterministic — a hallucinated 250% leftover cannot invent calories,
/// and applying a second leftover photo re-bases on the ORIGINAL totals
/// so repeated corrections never compound.
library;

import 'dart:convert';

import 'coerce.dart';
import 'shared_generated.dart';

/// The compact original-analysis JSON embedded in the prompt: whitelisted
/// fields only (never the raw stored blob — it is untrusted and may be
/// huge), capped to the shared wire bound.
String compactOriginalAnalysis(Map<String, dynamic> analysis) {
  final base = leftoverBase(analysis);
  final items = safeFoodItems(base);
  final compact = <String, dynamic>{
    'meal_description': '${base['meal_description'] ?? 'Meal'}',
    'total_calories': safeNumber(base['total_calories']),
    'total_protein_g': safeNumber(base['total_protein_g']),
    'total_carbs_g': safeNumber(base['total_carbs_g']),
    'total_fat_g': safeNumber(base['total_fat_g']),
    'food_items': [
      for (final it in items)
        {
          'name': '${it['name'] ?? '?'}',
          'estimated_calories': safeNumber(it['estimated_calories']),
        },
    ],
  };
  var out = jsonEncode(compact);
  if (out.length > SharedConstants.apiLeftoverMaxChars) {
    // Oversized almost always means absurd item names; drop items first,
    // then hard-truncate the description — the model still gets totals.
    compact['food_items'] = const [];
    out = jsonEncode(compact);
    if (out.length > SharedConstants.apiLeftoverMaxChars) {
      compact['meal_description'] = ('${compact['meal_description']}')
          .substring(0, 200);
      out = jsonEncode(compact);
    }
  }
  return out;
}

/// The totals a (re-)application derives from: the FIRST application
/// stashes the pre-deduction analysis under `leftover.original`; later
/// applications read it back so fractions always mean "of the original
/// meal", never "of the already-deducted remainder".
Map<String, dynamic> leftoverBase(Map<String, dynamic> analysis) {
  final leftover = analysis['leftover'];
  if (leftover is Map && leftover['original'] is Map) {
    // STALENESS GUARD: the snapshot only speaks for the meal while the
    // stored totals still match what that application produced. A manual
    // edit afterwards (meal editor, chat correction) is the user's newer
    // truth — re-basing on the old snapshot would silently discard it
    // (pressure-test find).
    final stamped = leftover['applied_total'];
    if (stamped == null ||
        safeNumber(stamped) != safeNumber(analysis['total_calories'])) {
      // No stamp (a pre-guard row) or totals that no longer match the
      // application: the CURRENT analysis is the user's newer truth —
      // protecting a manual edit beats snapshot fidelity.
      return analysis;
    }
    return (leftover['original'] as Map).cast<String, dynamic>();
  }
  return analysis;
}

double _clamp01(num v) => v.toDouble().clamp(0.0, 1.0);

/// One item's surviving fraction from the model reply, by exact-then-
/// case-insensitive name match; falls back to the overall fraction.
double _leftFractionFor(
    String name, List<Map<String, dynamic>> modelItems, double overall) {
  // safeNumberOr (not safeNumber): an UNCOERCIBLE fraction must fall back
  // to the meal-level estimate — reading it as 0 silently declared the
  // item fully eaten and inflated the day (pressure-test find).
  double? pick(Map<String, dynamic> m) {
    final v = safeNumberOr(m['left_fraction'], null);
    return v == null ? null : _clamp01(v);
  }

  for (final m in modelItems) {
    if ('${m['name']}' == name) return pick(m) ?? overall;
  }
  final lower = name.toLowerCase();
  for (final m in modelItems) {
    if ('${m['name']}'.toLowerCase() == lower) return pick(m) ?? overall;
  }
  return overall;
}

class LeftoverResult {
  const LeftoverResult({
    required this.adjusted,
    required this.eatenFraction,
    required this.deductedKcal,
    required this.sameMeal,
    required this.confidence,
    this.note,
  });

  final Map<String, dynamic> adjusted;

  /// Share of the ORIGINAL calories now counted as eaten, 0..1.
  final double eatenFraction;
  final int deductedKcal;
  final bool sameMeal;
  final double confidence;
  final String? note;
}

/// Apply a model leftover estimate to a meal's analysis. Returns null when
/// the reply carries no usable fraction (the flow shows an error rather
/// than guessing). Never throws on hostile shapes.
LeftoverResult? applyLeftover(
    Map<String, dynamic> current, Map<String, dynamic> modelOut,
    {required String leftoverPhotoMd5, required String appliedAtIso}) {
  final base = leftoverBase(current);
  final baseTotal = safeNumber(base['total_calories']).toDouble();

  final rawOverall = modelOut['leftover_fraction'];
  // The reply schema uses 'items' (not the analysis's 'food_items').
  final rawItems = modelOut['items'];
  final modelItems = rawItems is List
      ? rawItems
          .whereType<Map>()
          .map((m) => m.cast<String, dynamic>())
          .toList()
      : const <Map<String, dynamic>>[];
  double? overall = rawOverall is num && !(rawOverall is double && !rawOverall.isFinite)
      ? _clamp01(rawOverall)
      : null;
  // USABILITY gate: at least one coercible fraction must exist somewhere.
  // Items whose fractions are all junk ('1.0'-as-string, bools) used to
  // slip past the emptiness check and return a 'successful' zero
  // deduction — a guess dressed as an answer (pressure-test find).
  final anyItemFraction = modelItems
      .any((m) => safeNumberOr(m['left_fraction'], null) != null);
  if (overall == null && !anyItemFraction) return null;

  final baseItems = safeFoodItems(base);
  // Per-item eaten calories; items unknown to the model fall back to the
  // overall fraction (or, with no overall, are treated as eaten — the
  // model was told invisible items count as eaten).
  final fallback = overall ?? 0.0;
  final adjustedItems = <Map<String, dynamic>>[];
  double eatenSum = 0, baseSum = 0;
  for (final it in baseItems) {
    final name = '${it['name'] ?? '?'}';
    final cal = safeNumber(it['estimated_calories']).toDouble();
    final left = _leftFractionFor(name, modelItems, fallback);
    final eaten = cal * (1 - left);
    baseSum += cal;
    eatenSum += eaten;
    adjustedItems.add({
      ...it,
      'estimated_calories': eaten.round(),
      'leftover_left_fraction': double.parse(left.toStringAsFixed(2)),
    });
  }

  // Meal-level eaten fraction: from item math when items carry calories,
  // else from the overall fraction directly.
  final double eatenFraction;
  if (baseSum > 0) {
    eatenFraction = (eatenSum / baseSum).clamp(0.0, 1.0);
  } else if (overall != null) {
    eatenFraction = (1 - overall).clamp(0.0, 1.0);
  } else {
    return null;
  }

  final newTotal = (baseTotal * eatenFraction).round();
  // A hostile/corrupt base can be negative; clamp(0, negative) THROWS
  // (pressure-test find). A deduction is never negative and never more
  // than the base.
  final baseRounded = baseTotal.round();
  final deducted = baseRounded <= 0
      ? 0
      : (baseRounded - newTotal).clamp(0, baseRounded);
  num scaledMacro(String key) {
    final v = safeNumber(base[key]).toDouble() * eatenFraction;
    return double.parse(v.toStringAsFixed(1));
  }

  final adjusted = <String, dynamic>{
    ...current,
    'total_calories': newTotal,
    'total_protein_g': scaledMacro('total_protein_g'),
    'total_carbs_g': scaledMacro('total_carbs_g'),
    'total_fat_g': scaledMacro('total_fat_g'),
    if (adjustedItems.isNotEmpty) 'food_items': adjustedItems,
    'leftover': {
      'applied_at': appliedAtIso,
      // What THIS application stored; leftoverBase compares against it to
      // notice a later manual edit.
      'applied_total': newTotal,
      'leftover_photo_md5': leftoverPhotoMd5,
      'eaten_fraction': double.parse(eatenFraction.toStringAsFixed(2)),
      'note': modelOut['note'] is String ? modelOut['note'] : null,
      // The pre-deduction snapshot future applications re-base on. Taken
      // from the CURRENT base (not `current`) so a re-application keeps
      // the true original, and stripped of any nested leftover block.
      'original': {
        for (final e in base.entries)
          if (e.key != 'leftover') e.key: e.value,
      },
    },
  };

  final conf = modelOut['confidence'];
  return LeftoverResult(
    adjusted: adjusted,
    eatenFraction: eatenFraction,
    deductedKcal: deducted,
    sameMeal: isFoodTruthyBool(modelOut['same_meal']),
    confidence:
        conf is num && conf.toDouble().isFinite ? _clamp01(conf) : 0.5,
    note: modelOut['note'] is String ? modelOut['note'] as String : null,
  );
}

/// Python-truthiness for same_meal, defaulting TRUE when absent — a model
/// that omitted the field but returned fractions is answering the question.
bool isFoodTruthyBool(dynamic v) {
  if (v == null) return true;
  final parsed = parseBoolish(v);
  return parsed ?? true;
}

// ─── Automatic detection (2026-08-05) ────────────────────────────────
// The watcher's own analysis call carries a compact list of the day's
// meals; the model may answer {leftover_of: i, ...} instead of a new
// meal. Everything here is the DETERMINISTIC half of that contract.

/// Auto-apply only above this confidence: below it a wrong match would
/// silently shrink a real meal, while falling through merely logs a new
/// meal the manual flow can still fix.
const double kAutoLeftoverMinConfidence = 0.75;

/// The prompt carries at most this many of TODAY's most recent meals —
/// leftovers come minutes-to-hours after the original, and a shorter
/// list keeps the model's matching sharp.
const int kAutoLeftoverMaxCandidates = 5;

/// The RECENT MEALS block appended to the photo prompt. Indexes are the
/// caller's list positions — the SAME snapshot must be used to resolve
/// the reply (spec §4.2 snapshot rule). Empty list → empty string (the
/// prompt section tells the model to ignore itself in that case).
String formatRecentMealsBlock(List<Map<String, dynamic>> compacts) {
  if (compacts.isEmpty) return '';
  final lines = <String>['', 'RECENT MEALS (today, for the leftover check):'];
  for (var i = 0; i < compacts.length; i++) {
    final c = compacts[i];
    final items = c['food_items'];
    final itemBits = items is List
        ? [
            for (final it in items.whereType<Map>())
              '${it['name'] ?? '?'} (~${safeNumber(it['estimated_calories']).round()} kcal)'
          ]
        : const <String>[];
    lines.add('[$i] ${c['time'] ?? ''} — ${c['meal_description'] ?? 'Meal'} '
        '(~${safeNumber(c['total_calories']).round()} kcal)'
        '${itemBits.isEmpty ? '' : ': ${itemBits.join(', ')}'}');
  }
  return lines.join('\n');
}

/// The compact shape one candidate meal contributes to the block AND to
/// the server payload (whitelisted, like compactOriginalAnalysis).
Map<String, dynamic> recentMealCompact(
        {required String time, required Map<String, dynamic> analysis}) =>
    {
      'time': time,
      'meal_description': '${analysis['meal_description'] ?? 'Meal'}',
      'total_calories': safeNumber(analysis['total_calories']),
      'food_items': [
        for (final it in safeFoodItems(analysis))
          {
            'name': '${it['name'] ?? '?'}',
            'estimated_calories': safeNumber(it['estimated_calories']),
          },
      ],
    };

/// Gate an analysis reply's automatic-leftover claim: returns the VALID
/// candidate index, or null when the reply is not a confident, usable
/// leftover verdict (→ caller saves a normal meal). Never throws.
int? autoLeftoverIndex(Map<String, dynamic> analysis,
    {required int candidateCount}) {
  final raw = analysis['leftover_of'];
  if (raw is! num || raw is bool) return null;
  final idx = raw.toInt();
  if (idx < 0 || idx >= candidateCount || idx != raw) return null;
  final conf = analysis['confidence'];
  final confidence =
      conf is num && conf.toDouble().isFinite ? conf.toDouble() : 0.0;
  if (confidence < kAutoLeftoverMinConfidence) return null;
  // Usability: applyLeftover needs a fraction or items to work with.
  final frac = analysis['leftover_fraction'];
  final items = analysis['items'];
  final hasFraction = frac is num && !(frac is double && !frac.isFinite);
  final hasItems = items is List && items.whereType<Map>().isNotEmpty;
  if (!hasFraction && !hasItems) return null;
  return idx;
}
