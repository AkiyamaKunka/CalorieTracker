/// NL executor — the hardened text-message pipeline (spec §4).
///
/// Direct port of handle_text_message / _normalize_nl_actions and the five
/// intent handlers (telegram_bot.py:3953-4376). The Python originals were
/// hardened after a 2026-07-16 production crash-loop on a compound
/// correction+delete message; every guard below is pinned by the tests named
/// in spec §4.10 and mirrored under test/nl/.
library;

import 'dart:math' as math;

import '../../core/coerce.dart';
import '../../core/contracts.dart';
import '../../core/prompts.dart';
import '../../core/shared_generated.dart';
import '../settings/app_settings.dart';

/// Spec §1.2 / §8: TEXT_EDIT_WINDOW_DAYS (telegram_bot.py:161), from shared/.
const int textEditWindowDays = SharedConstants.textEditWindowDaysDefault;

/// Spec §4.1: NL_MAX_ACTIONS (telegram_bot.py:4181), from shared/.
const int nlMaxActions = SharedConstants.nlMaxActions;

/// Spec §4.8 clamps (telegram_bot.py:2971-2973), from shared/.
const num activityKcalMax = SharedConstants.activityKcalMax;
const num activityStepsMax = SharedConstants.activityStepsMax;
const num activityKmMax = SharedConstants.activityKmMax;

/// Spec §4.7 / §7.1 weight bounds (nutrition.py:429-430), from shared/.
const num minWeightKg = SharedConstants.weightMinKg;
const num maxWeightKg = SharedConstants.weightMaxKg;

const double _lbToKg = 0.45359237; // nutrition.py _LB_TO_KG

// nutrition.py:416-427. The (?<![\w.]) lookbehind stops the number from
// starting mid-token ("1e72" cannot have its exponent read as 72 kg); the
// (?![a-z]) lookahead stops the unit matching mid-token ("kilometers" cannot
// be read as kilograms; the regex is case-insensitive so A-Z is covered too).
final RegExp _weightUnitRe = RegExp(
  r'(?<![\w.])(\d+(?:\.\d+)?)\s*(kgs?|kilograms?|kilos?|lbs?|pounds?)(?![a-z])',
  caseSensitive: false,
);
// Bare number guarded by an explicit weigh(ed/s/t) keyword, read as kg.
final RegExp _weightKeywordRe = RegExp(
  r'weigh(?:ed|s|t)?\D{0,12}?(?<![\w.])(\d+(?:\.\d+)?)',
  caseSensitive: false,
);

const Set<String> _handlerIntents = {
  'correction',
  'delete',
  'new_meal',
  'log_weight',
  'log_activity',
};

NlExecutor createExecutor(
        MealsDao dao, AnalyzerService analyzer, AppSettings settings) =>
    DefaultNlExecutor(dao, analyzer, settings);

/// Python truthiness (spec §3.5 isFoodTruthy): false, 0, 0.0, '', [], {},
/// null → false; everything else → true. Consumers use raw truthiness on
/// is_food/reason, never `== true` (telegram_bot.py:842).
bool isTruthy(dynamic v) {
  if (v == null || v == false) return false;
  if (v is num) return v != 0;
  if (v is String) return v.isNotEmpty;
  if (v is Iterable) return v.isNotEmpty;
  if (v is Map) return v.isNotEmpty;
  return true;
}

/// Python `a or b`: [v] when truthy, else [fallback] (label/card formatting
/// uses `analysis.get(...) or 0` semantics — spec §4.5, §5.4).
dynamic _orElse(dynamic v, dynamic fallback) => isTruthy(v) ? v : fallback;

/// _coerce_meal_index (spec §4.3, telegram_bot.py:3953-3971): bool → null;
/// int → itself; float → int only when integral; string → trimmed int parse;
/// anything else null. A hallucinated index is "no match", never a crash.
int? coerceMealIndex(dynamic value) {
  if (value is bool) return null;
  if (value is int) return value;
  if (value is double) {
    if (!value.isFinite || value != value.truncateToDouble()) return null;
    return value.toInt();
  }
  if (value is String) return int.tryParse(value.trim());
  return null;
}

/// _normalize_nl_actions (spec §4.1, telegram_bot.py:4184-4237): coerce any
/// parsed model response — single object, {"intent":"multi","actions":[...]},
/// or the bare JSON ARRAY that crash-looped production on 2026-07-16 — into
/// an ordered list of action maps. Unusable input yields [], never a throw.
List<Map> normalizeActions(dynamic result) {
  List<dynamic> items;
  if (result is Map) {
    final actions = result['actions'];
    // Only honor "actions" when the wrapper is the designed multi shape or
    // has no recognized single intent of its own — a real single intent
    // carrying a hallucinated decomposition list must win. The `is String`
    // check is the unhashable-intent guard: a list/dict-valued intent reads
    // as "not recognized", it must not throw (spec §4.1 rule 1).
    final intent = result['intent'];
    final recognized = intent is String && _handlerIntents.contains(intent);
    final useActions = actions is List &&
        actions.isNotEmpty &&
        (intent == 'multi' || !recognized);
    items = useActions ? actions : <dynamic>[result];
  } else if (result is List) {
    items = result; // the bare-array production shape (spec §4.1 rule 2)
  } else {
    return const [];
  }

  // Rule 3: drop non-map items. Rule 4: cap at NL_MAX_ACTIONS, keep first 5.
  var usable = items.whereType<Map>().toList();
  if (usable.length > nlMaxActions) usable = usable.sublist(0, nlMaxActions);

  // Rule 5: duplicate-delete merging — there is ONE pending delete-
  // confirmation slot, so all list-typed meal_indices concatenate into the
  // FIRST delete action and the rest are dropped (telegram_bot.py:4224-4236).
  final deletes = usable.where((a) => a['intent'] == 'delete').toList();
  if (deletes.length > 1) {
    final merged = <dynamic>[];
    for (final a in deletes) {
      final indices = a['meal_indices'];
      if (indices is List) merged.addAll(indices);
    }
    deletes.first['meal_indices'] = merged;
    usable = usable
        .where((a) => a['intent'] != 'delete' || identical(a, deletes.first))
        .toList();
  }
  return usable;
}

/// parse_weight_kg (spec §4.7, nutrition.py:433-460): explicit kg/lb units
/// (lb × 0.45359237) or a bare number guarded by a weigh-keyword; rounded to
/// 1 decimal; only accepted within [30, 300] kg.
double? parseWeightKg(dynamic text) {
  if (text is! String) return null;
  for (final m in _weightUnitRe.allMatches(text)) {
    final value = double.tryParse(m.group(1)!);
    if (value == null) continue;
    final unit = m.group(2)!.toLowerCase();
    final raw =
        (unit.startsWith('lb') || unit.startsWith('pound')) ? value * _lbToKg : value;
    final kg = _round1(raw);
    if (kg >= minWeightKg && kg <= maxWeightKg) return kg;
  }
  final m = _weightKeywordRe.firstMatch(text);
  if (m != null) {
    final kg = _round1(double.parse(m.group(1)!));
    if (kg >= minWeightKg && kg <= maxWeightKg) return kg;
  }
  return null;
}

/// meal_calorie_mismatch (spec §3.5, utils.py:75-97): item sum when it
/// contradicts the stored total by more than max(100, 20%), else null.
/// Missing/non-numeric data is treated as consistent.
int? mealCalorieMismatch(dynamic analysis) {
  num itemSum = 0;
  var counted = 0;
  for (final item in safeFoodItems(analysis)) {
    final cal = _asNumber(item['estimated_calories']);
    if (cal != null) {
      itemSum += cal;
      counted++;
    }
  }
  final total = analysis is Map ? _asNumber(analysis['total_calories']) : null;
  if (counted == 0 || itemSum <= 0 || total == null) return null;
  if ((total - itemSum).abs() >
      math.max(
          SharedConstants.mealMismatchMinKcal,
          SharedConstants.mealMismatchPct * math.max(total, itemSum))) {
    return itemSum.toInt();
  }
  return null;
}

/// Single-meal result card, meal-local portion of format_food_result
/// (spec §5.4, telegram_bot.py:2875-2919) rendered as plain text. The
/// running daily-totals block is the reports module's concern; the UI
/// appends it separately.
String formatFoodResult(Map<String, dynamic> analysis) {
  if (!isTruthy(analysis['is_food'])) return '🚫 No food detected in this photo.';
  final lines = <String>['🍽️ ${analysis['meal_description'] ?? 'Unknown meal'}\n'];
  for (final item in safeFoodItems(analysis)) {
    dynamic cals =
        item.containsKey('estimated_calories') ? item['estimated_calories'] : '?';
    // safe_number for numerics (JSON Infinity/NaN must never render as
    // 'Infinity kcal'; Python bools are ints there, so bool → 0); a
    // non-numeric string still displays raw (spec §3.5 display fallback).
    if (cals is bool) {
      cals = 0;
    } else if (cals is num) {
      cals = safeNumber(cals);
    }
    lines.add('  • ${item['name'] ?? '?'}: ~$cals kcal');
    lines.add('    P:${safeNumber(item['protein_g'])}g | '
        'C:${safeNumber(item['carbs_g'])}g | F:${safeNumber(item['fat_g'])}g');
  }
  final total = _orElse(analysis['total_calories'], '?'); // raw `or "?"`
  lines.add('\n📊 This meal: ~$total kcal');
  lines.add('🥩 P: ${_orElse(analysis['total_protein_g'], 0)}g | '
      '🍞 C: ${_orElse(analysis['total_carbs_g'], 0)}g | '
      '🧈 F: ${_orElse(analysis['total_fat_g'], 0)}g');
  final itemSum = mealCalorieMismatch(analysis);
  if (itemSum != null) {
    lines.add('⚠️ Item calories sum to ~$itemSum kcal but the meal total is '
        '~$total kcal — reply with a correction if this looks wrong.');
  }
  return lines.join('\n');
}

/// meals_list prompt lines in the exact server format (spec §1.2,
/// telegram_bot.py:4265-4281). Stored analyses are untrusted: safeFoodItems
/// plus stringification mean one poison row cannot break prompt building.
String buildMealsList(List<Meal> meals) {
  if (meals.isEmpty) return 'No meals logged recently.';
  final lines = <String>[];
  for (var i = 0; i < meals.length; i++) {
    final a = meals[i].analysis;
    final desc = a['meal_description'] ?? 'Unknown';
    final cal = a['total_calories'] ?? 0;
    final items =
        safeFoodItems(a).map((it) => '${it['name'] ?? '?'}').join(', ');
    lines.add(
        '[$i] Date: ${meals[i].date} | Meal: $desc (~$cal kcal) — Items: $items');
  }
  return lines.join('\n');
}

class DefaultNlExecutor implements NlExecutor {
  DefaultNlExecutor(this.dao, this.analyzer, this.settings);

  final MealsDao dao;
  final AnalyzerService analyzer;
  final AppSettings settings;

  /// One pending confirmation slot (spec §4.5: one per chat; the app's modal
  /// dialog replaces the server's nonce/TTL machinery). id → display label.
  final Map<int, String> _pendingDeleteLabels = {};

  @override
  Future<List<NlReply>> handleText(String userText) async {
    // handle_text_message_safe parity (spec §4.9, telegram_bot.py:4359-4376):
    // no exception may escape into the caller's event loop.
    try {
      return await _handleTextInner(userText);
    } catch (_) {
      return const [
        NlReply(
            '❌ Something went wrong handling that message. Please try again.')
      ];
    }
  }

  Future<List<NlReply>> _handleTextInner(String userText) async {
    // Spec §4 step 2 (telegram_bot.py:4253-4261): during a daily-quota pause,
    // refuse with the pause summary and make NO model call.
    if (settings.isQuotaPaused) {
      return [NlReply(_quotaPauseMessage(settings.quotaPauseUntil!))];
    }

    // Snapshot: last TEXT_EDIT_WINDOW_DAYS of food meals, oldest first
    // (spec §1.2). Every index in every action resolves against THIS list.
    final meals = await dao.recentMeals(days: textEditWindowDays);
    final prompt = buildPrompt(meals, userText, DateTime.now());

    final Map<String, dynamic>? result;
    try {
      result = await analyzer.textIntent(prompt);
    } catch (_) {
      return const [NlReply('❌ Error contacting AI. Please try again.')];
    }
    if (result == null) {
      // The analyzer seam folds parse + transport failures into null
      // (contract: textIntent never throws); spec §4 step 4 wording.
      return const [NlReply('❌ Error contacting AI. Please try again.')];
    }
    return executeParsed(result, userText, meals);
  }

  /// Describe-only path (app-only, spec §9): analyze [text] as a brand-new
  /// meal and hand the analysis back for preview. NOTHING is saved.
  ///
  /// The prompt is built with an EMPTY meals list on purpose — the shared
  /// prompt's own rule ("If the user describes food but meals_list is empty,
  /// it MUST be a new_meal") then makes the intent deterministic, so no new
  /// prompt and no server divergence. Multi-action replies are normalized
  /// the same way handleText does, and the first new_meal wins.
  @override
  Future<DescribeOutcome> describeMeal(String text) async {
    final userText = text.trim();
    if (userText.isEmpty) {
      return const DescribeOutcome(error: 'Type what you ate first.');
    }
    if (settings.isQuotaPaused) {
      return DescribeOutcome(
          error: _quotaPauseMessage(settings.quotaPauseUntil!));
    }
    final prompt = buildPrompt(const [], userText, DateTime.now());
    final Map<String, dynamic>? result;
    try {
      result = await analyzer.textIntent(prompt);
    } catch (_) {
      return const DescribeOutcome(
          error: '❌ Error contacting AI. Please try again.');
    }
    if (result == null) {
      return const DescribeOutcome(
          error: '❌ Error contacting AI. Please try again.');
    }
    // Reuse the hardened normalization: bare arrays, {actions:[...]},
    // single objects and junk all collapse to a list of actions (§4.1).
    // A compound description ("eggs for breakfast, then a salad for lunch")
    // legitimately yields SEVERAL new_meals — preview the first, but say so,
    // because dropping the rest in silence under-counts the day invisibly.
    final meals = <Map<String, dynamic>>[];
    for (final action in normalizeActions(result)) {
      if (action['intent'] != 'new_meal') continue;
      final dynamic analysis = action['analysis'];
      if (analysis is! Map || !isTruthy(analysis['is_food'])) continue;
      final sanitized = <String, dynamic>{
        for (final e in analysis.entries) '${e.key}': e.value,
      };
      if (sanitized.containsKey('food_items')) {
        sanitized['food_items'] = safeFoodItems(sanitized);
      }
      meals.add(sanitized);
    }
    if (meals.isEmpty) {
      return const DescribeOutcome(
          error: "🚫 I couldn't detect food in that description.");
    }
    return DescribeOutcome(
      analysis: meals.first,
      warning: meals.length > 1
          ? 'That described ${meals.length} meals — only the first is shown. '
              'Describe the others one at a time.'
          : null,
    );
  }

  /// The full TEXT_HANDLER_PROMPT with the five placeholders (spec §1.2).
  /// Note the dietary profile is deliberately NOT appended here — spec §1.3
  /// pins it to the photo prompt only.
  String buildPrompt(List<Meal> meals, String userText, DateTime now) {
    final today = DateTime(now.year, now.month, now.day);
    final yesterday = DateTime(now.year, now.month, now.day - 1);
    return textHandlerPrompt(
      mealsList: buildMealsList(meals),
      userMessage: userText,
      today: _isoDate(today),
      weekday: _weekdayNames[today.weekday - 1],
      yesterday: _isoDate(yesterday),
    );
  }

  /// Post-parse pipeline: normalize → dispatch → per-action containment
  /// (spec §4.1/§4.2/§4.9). Public and dynamically typed because production
  /// models have returned bare JSON arrays despite the prompt (spec §4.1);
  /// [snapshot] must be the same list the prompt showed so indices resolve
  /// to DB ids that earlier actions can never shift (spec §4.2).
  Future<List<NlReply>> executeParsed(
      dynamic result, String userText, List<Meal> snapshot) async {
    final actions = normalizeActions(result);
    if (actions.isEmpty) {
      return const [
        NlReply("❌ I couldn't work out what to do with that. "
            'Try one request at a time, e.g. “change meal 2 to roast duck rice”.')
      ];
    }

    final replies = <NlReply>[];
    var failures = 0;
    for (final action in actions) {
      try {
        replies.add(await _dispatch(action, userText, snapshot));
      } catch (_) {
        // One malformed action must not abort its siblings (spec §4.9).
        failures++;
      }
    }
    if (failures == actions.length) {
      // The wording must never claim partial success (spec §4.9).
      replies.add(NlReply(failures == 1
          ? '❌ That request failed. Please try again.'
          : '❌ All $failures requested actions failed. Please try again.'));
    } else if (failures > 0) {
      replies.add(NlReply(
          '⚠️ $failures of ${actions.length} requested action(s) failed — the rest were applied.'));
    }
    return replies;
  }

  Future<NlReply> _dispatch(Map action, String text, List<Meal> meals) {
    // Non-string / unknown intents fall back to chat (spec §4.2); switching
    // on the dynamic value cannot throw for list/map-typed intents.
    switch (action['intent']) {
      case 'correction':
        return _correction(text, meals, action);
      case 'delete':
        return _delete(text, meals, action);
      case 'new_meal':
        return _newMeal(action);
      case 'log_weight':
        return _logWeight(text, action);
      case 'log_activity':
        return _logActivity(action);
      default:
        return _chat(action);
    }
  }

  /// _nl_correction (spec §4.4, telegram_bot.py:3974-4032).
  Future<NlReply> _correction(String text, List<Meal> meals, Map action) async {
    final mealIndex =
        coerceMealIndex(action.containsKey('meal_index') ? action['meal_index'] : 0);
    final dynamic newAnalysis =
        action.containsKey('analysis') ? action['analysis'] : <String, dynamic>{};
    final dynamic reason = action['reason'] ?? '';

    if (meals.isEmpty) {
      return const NlReply(
          '❌ Cannot correct because no meals are logged recently.');
    }
    if (mealIndex == null || mealIndex < 0 || mealIndex >= meals.length) {
      // meal_index is raw model output: truncate to 40 chars so hallucinated
      // markup cannot flood the reply (spec §4.4.2; app renders plain text).
      var shown = '${action['meal_index']}';
      if (shown.length > 40) shown = shown.substring(0, 40);
      return NlReply(
          '❌ Invalid meal index ($shown). You have ${meals.length} recent meals.');
    }

    // Silent-delete guard (spec §4.4.3): an empty or non-food analysis would
    // overwrite the meal into a row every is_food view filters out — a
    // delete that skipped the delete confirmation. Refuse it.
    if (newAnalysis is! Map || !isTruthy(newAnalysis['is_food'])) {
      return const NlReply(
          "❌ That correction didn't include a usable updated analysis, so I left "
          'the meal unchanged. Try restating it, e.g. “meal 2 was roast duck rice, ~780 kcal”.');
    }

    final sanitized = <String, dynamic>{
      for (final e in newAnalysis.entries) '${e.key}': e.value,
    };
    // A hostile food_items shape persisted here would crash every later
    // prompt build over the edit window; store maps only (spec §3.5).
    if (sanitized.containsKey('food_items')) {
      sanitized['food_items'] = safeFoodItems(sanitized);
    }

    // Compute EVERY reply input BEFORE the write: safeNumber means a
    // hallucinated string calorie can no longer throw after
    // updateMealAnalysis already ran (the user would be told "failed" for a
    // change that happened — spec §4.4.5).
    final old = meals[mealIndex].analysis;
    final oldCal = safeNumber(old['total_calories']);
    final newCal = safeNumber(sanitized['total_calories']);
    final oldDesc = '${old['meal_description'] ?? 'Unknown'}';
    final newDesc = '${sanitized['meal_description'] ?? oldDesc}';
    final diff = newCal - oldCal;
    final diffStr = diff > 0 ? '+$diff' : '$diff';

    // Update by the snapshot row's DB id, so a meal logged mid-conversation
    // cannot shift the target (spec §4.4.6). The DAO sets corrected=1
    // unconditionally (spec §2.4).
    await dao.updateMealAnalysis(meals[mealIndex].id, sanitized);

    final lines = <String>[
      '✏️ Corrected meal ${mealIndex + 1}!',
      '',
      '$oldDesc → $newDesc',
      '🔥 $oldCal kcal → $newCal kcal ($diffStr)',
    ];
    if (isTruthy(reason)) lines.add('\n💬 $reason');
    return NlReply(lines.join('\n'));
  }

  /// _nl_delete (spec §4.5, telegram_bot.py:4035-4094). Stages ids + labels
  /// and returns needsDeleteConfirmation — the executor NEVER deletes here;
  /// [confirmPendingDelete] performs the deletes after the modal confirm.
  Future<NlReply> _delete(String text, List<Meal> meals, Map action) async {
    final dynamic raw =
        action.containsKey('meal_indices') ? action['meal_indices'] : const [];
    List<dynamic> mealIndices;
    if (raw is List) {
      mealIndices = raw;
    } else if (raw is int) {
      // Honor a bare integer as [n]. In Python a scalar string "12" iterated
      // per character would stage meals 1 and 2 — anything non-list and
      // non-int means "no match". (Dart bool is not int, so the Python
      // bool-excluding check is implicit.)
      mealIndices = [raw];
    } else {
      mealIndices = const [];
    }
    final dynamic reason = action['reason'] ?? '';

    if (meals.isEmpty) {
      return const NlReply(
          '❌ Cannot delete because no meals are logged recently.');
    }
    if (mealIndices.isEmpty) {
      return const NlReply(
          "❌ Didn't catch which meals to delete. Try being more specific.");
    }

    // Coerce, de-duplicate, ascending, in-range only (spec §4.5.3); resolve
    // ids and labels from the SNAPSHOT the model indexed.
    final valid = <int>{};
    for (final x in mealIndices) {
      final i = coerceMealIndex(x);
      if (i != null) valid.add(i);
    }
    final ids = <int>[];
    final labels = <String>[];
    for (final index in valid.toList()..sort()) {
      if (index >= 0 && index < meals.length) {
        final meal = meals[index];
        final a = meal.analysis;
        ids.add(meal.id);
        labels.add('${a['meal_description'] ?? 'Unknown meal'} '
            '(${meal.date} ${meal.time}, '
            '~${_orElse(a['total_calories'], 0)} kcal)');
      }
    }
    if (ids.isEmpty) {
      return const NlReply("❌ Couldn't match those meals to the recent list.");
    }

    // One pending slot per chat (spec §4.5.4): staging a new delete
    // supersedes any older pending set.
    _pendingDeleteLabels
      ..clear()
      ..addEntries([
        for (var i = 0; i < ids.length; i++) MapEntry(ids[i], labels[i]),
      ]);

    final lines = <String>['🗑️ Delete ${ids.length} meal(s)?', ''];
    lines.addAll(labels.map((l) => '• $l'));
    if (isTruthy(reason)) lines.add('\n💬 $reason');
    lines.add('\nThis cannot be undone.');
    return NlReply(lines.join('\n'),
        needsDeleteConfirmation: true,
        pendingDeleteIds: ids,
        pendingDeleteLabels: labels);
  }

  @override
  Future<String> confirmPendingDelete(List<int> mealIds) async {
    if (mealIds.isEmpty) return '👍 Cancelled — nothing was deleted.';
    final labels = <String>[];
    for (final id in mealIds) {
      // deleteMeal tombstones the ledger row (spec §2.4) so the backfill
      // scan never resurrects the photo.
      await dao.deleteMeal(id);
      labels.add(_pendingDeleteLabels[id] ?? 'meal #$id');
    }
    _pendingDeleteLabels.clear();
    return [
      '🗑️ Deleted ${mealIds.length} meal(s):',
      ...labels.map((l) => '• $l'),
    ].join('\n');
  }

  /// _nl_new_meal (spec §4.6, telegram_bot.py:4097-4109).
  Future<NlReply> _newMeal(Map action) async {
    final dynamic analysis =
        action.containsKey('analysis') ? action['analysis'] : <String, dynamic>{};
    if (analysis is! Map || !isTruthy(analysis['is_food'])) {
      return const NlReply("🚫 I couldn't detect food in that description.");
    }
    final sanitized = <String, dynamic>{
      for (final e in analysis.entries) '${e.key}': e.value,
    };
    if (sanitized.containsKey('food_items')) {
      sanitized['food_items'] = safeFoodItems(sanitized);
    }
    final now = DateTime.now();
    await dao.saveMeal(Meal(
      id: 0, // assigned by the DAO
      date: _isoDate(now),
      time: _time12(now),
      timestamp: now.toIso8601String(),
      source: MealSource.manualText, // spec §4.6
      imageHash: '',
      fileId: '',
      analysis: sanitized,
    ));
    return NlReply('✅ Added new manual meal:\n\n${formatFoodResult(sanitized)}');
  }

  /// _nl_log_weight (spec §4.7, telegram_bot.py:4112-4128): deterministic
  /// text parse first; the model's weight_kg is only a bounded-numeric
  /// fallback (string numerics are deliberately NOT trusted).
  Future<NlReply> _logWeight(String text, Map action) async {
    var kg = parseWeightKg(text);
    if (kg == null) {
      final field = safeNumber(action['weight_kg']);
      if (field >= minWeightKg && field <= maxWeightKg) kg = _round1(field);
    }
    if (kg == null) {
      return const NlReply("⚖️ I couldn't read a valid body weight (30–300 kg). "
          'Try “I weigh 72.5 kg”.');
    }
    final date = _isoDate(DateTime.now());
    // Upsert-by-day (spec §2.1 body_weight: re-log overwrites).
    await dao.saveBodyWeight(date, kg);
    return NlReply('⚖️ Logged ${_g(kg)} kg for $date.');
  }

  /// _nl_log_activity (spec §4.8, telegram_bot.py:4131-4158).
  Future<NlReply> _logActivity(Map action) async {
    final kcal = _clamp(safeNumber(action['active_calories']), 0, activityKcalMax);
    final steps =
        _clamp(safeNumber(action['steps']), 0, activityStepsMax).round();
    final km = _clamp(safeNumber(action['distance_km']), 0, activityKmMax);
    if (kcal <= 0 && steps <= 0 && km <= 0) {
      return const NlReply("🏃 I couldn't find any activity numbers to log. "
          'Try “burned 450 kcal running 5 km”.');
    }
    final date = _isoDate(DateTime.now());
    // Zeros store as null; steps ride in raw={"steps": n} inside the DAO.
    await dao.saveActivity(date,
        activeCalories: kcal > 0 ? kcal : null,
        steps: steps > 0 ? steps : null,
        distanceKm: km > 0 ? km.toDouble() : null);
    final bits = <String>[
      if (kcal > 0) '${_comma(kcal.round())} kcal',
      if (steps > 0) '${_comma(steps)} steps',
      if (km > 0) '${_g(km)} km',
    ];
    return NlReply('🏃 Logged activity: ${bits.join(' · ')} ($date).');
  }

  /// _nl_chat (spec §4.9, telegram_bot.py:4161-4168): a null/non-string/blank
  /// reply must never render an empty bubble.
  Future<NlReply> _chat(Map action) async {
    final reply = action['reply'];
    if (reply is String && reply.trim().isNotEmpty) return NlReply(reply);
    return const NlReply(
        "I'm not sure what you mean. Try describing a meal or correction!");
  }
}

/// The quota-pause refusal (spec §3.3/§4, telegram_bot.py:951-966 wording,
/// 4253-4261 framing) as plain text.
String _quotaPauseMessage(DateTime until) {
  final remainingSeconds =
      math.max(0, until.difference(DateTime.now()).inSeconds);
  final String remaining;
  if (remainingSeconds < 60) {
    remaining = '${remainingSeconds}s';
  } else if (remainingSeconds < 3600) {
    remaining = '${remainingSeconds ~/ 60}m';
  } else {
    remaining = '${remainingSeconds ~/ 3600}h'
        '${((remainingSeconds % 3600) ~/ 60).toString().padLeft(2, '0')}m';
  }
  final untilStr = '${_isoDate(until)}T'
      '${until.hour.toString().padLeft(2, '0')}:'
      '${until.minute.toString().padLeft(2, '0')}';
  return '⏸️ Gemini is paused right now.\n\n'
      'Gemini daily free-tier quota is paused until $untilStr '
      '(about $remaining remaining).\n\n'
      'Text corrections and manual meal parsing need Gemini too, '
      'so I did not send this request.';
}

const List<String> _weekdayNames = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
];

String _isoDate(DateTime d) => '${d.year.toString().padLeft(4, '0')}-'
    '${d.month.toString().padLeft(2, '0')}-'
    '${d.day.toString().padLeft(2, '0')}';

/// strftime("%I:%M %p") parity: 12-hour, zero-padded, space, AM/PM (spec §2.2).
String _time12(DateTime t) {
  final h = t.hour % 12 == 0 ? 12 : t.hour % 12;
  final ampm = t.hour < 12 ? 'AM' : 'PM';
  return '${h.toString().padLeft(2, '0')}:'
      '${t.minute.toString().padLeft(2, '0')} $ampm';
}

/// Python round(x, 1) equivalent for weight display/storage.
double _round1(num v) => (v * 10).round() / 10;

num _clamp(num v, num lo, num hi) => v < lo ? lo : (v > hi ? hi : v);

/// Python %g: shortest repr — integral doubles drop the ".0".
String _g(num v) {
  if (v is double && v.isFinite && v == v.truncateToDouble()) {
    return v.toInt().toString();
  }
  return v.toString();
}

/// Python f"{n:,}" thousands separators.
String _comma(int n) {
  final s = n.abs().toString();
  final buf = StringBuffer(n < 0 ? '-' : '');
  for (var i = 0; i < s.length; i++) {
    if (i > 0 && (s.length - i) % 3 == 0) buf.write(',');
    buf.write(s[i]);
  }
  return buf.toString();
}

/// utils._as_number bounds (spec §3.5): null instead of a default, so the
/// mismatch check can distinguish "no data" from zero.
num? _asNumber(dynamic v) {
  if (v is bool || v is! num) return null;
  final d = v.toDouble();
  if (d.isNaN || d.isInfinite || d <= -1e9 || d >= 1e9) return null;
  return v;
}
