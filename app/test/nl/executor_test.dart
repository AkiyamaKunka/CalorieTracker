/// NL executor pins (spec §4.10): Dart mirrors of the named Python tests in
/// tests/test_telegram_bot.py. The compound pins reproduce the exact
/// 2026-07-16 production crash-loop payloads.
library;

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/nl/executor.dart';
import 'package:calorie_tracker/services/settings/app_settings.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

const Map<String, dynamic> roastDuckAnalysis = {
  'is_food': true,
  'meal_description': '烧鸭饭',
  'total_calories': 780,
  'total_protein_g': 35,
  'total_carbs_g': 90,
  'total_fat_g': 28,
  'food_items': [
    {'name': '烧鸭饭', 'estimated_calories': 780, 'protein_g': 35, 'carbs_g': 90, 'fat_g': 28},
  ],
};

void main() {
  late FakeDao dao;
  late FakeAnalyzer analyzer;
  late AppSettings settings;
  late DefaultNlExecutor exec;

  setUp(() async {
    dao = FakeDao();
    analyzer = FakeAnalyzer();
    settings = await testSettings();
    exec = createExecutor(dao, analyzer, settings) as DefaultNlExecutor;
  });

  /// Breakfast/lunch/dinner; returns (breakfastId, lunchId, dinnerId).
  (int, int, int) seedThreeMeals() => (
        dao.seed('白粥', 150, time: '08:00 AM'),
        dao.seed('面条', 550, time: '12:30 PM'),
        dao.seed('米饭', 400, time: '07:00 PM'),
      );

  group('normalizeActions (spec §4.1)', () {
    test('single object becomes a one-action list', () {
      final actions = normalizeActions({'intent': 'chat', 'reply': 'hi'});
      expect(actions, hasLength(1));
      expect(actions.single['reply'], 'hi');
    });

    test('multi object uses its actions list', () {
      final actions = normalizeActions({
        'intent': 'multi',
        'actions': [
          {'intent': 'chat', 'reply': 'a'},
          {'intent': 'chat', 'reply': 'b'},
        ],
      });
      expect(actions.map((a) => a['reply']), ['a', 'b']);
    });

    test('bare array is used as the items (production regression shape)', () {
      final actions = normalizeActions([
        {'intent': 'correction', 'meal_index': 1},
        {'intent': 'delete', 'meal_indices': [2]},
      ]);
      expect(actions.map((a) => a['intent']), ['correction', 'delete']);
    });

    test('dict with actions and an UNRECOGNIZED intent uses the actions', () {
      final actions = normalizeActions({
        'intent': 'do_stuff',
        'actions': [
          {'intent': 'chat', 'reply': 'x'},
        ],
      });
      expect(actions.single['reply'], 'x');
    });

    test('real single intent wins over hallucinated actions', () {
      // Mirrors test_nl_single_intent_wins_over_hallucinated_actions.
      final actions = normalizeActions({
        'intent': 'correction',
        'meal_index': 1,
        'actions': [
          {'step': 'recalculate'},
          {'step': 'update'},
        ],
      });
      expect(actions, hasLength(1));
      expect(actions.single['intent'], 'correction');
    });

    test('list/map-valued intent must not throw (unhashable-intent guard)', () {
      final actions = normalizeActions({
        'intent': [1, 2],
        'actions': [
          {'intent': 'chat', 'reply': 'from actions'},
        ],
      });
      expect(actions.single['reply'], 'from actions');
    });

    test('unusable shapes yield [] and non-map entries are dropped', () {
      expect(normalizeActions('just a string'), isEmpty);
      expect(normalizeActions(42), isEmpty);
      expect(normalizeActions(null), isEmpty);
      expect(normalizeActions([]), isEmpty);
      expect(normalizeActions(['a', 3]), isEmpty);
      expect(
        normalizeActions([
          'junk',
          {'intent': 'chat', 'reply': 'kept'},
          7,
        ]).single['reply'],
        'kept',
      );
    });

    test('caps at NL_MAX_ACTIONS keeping the first 5', () {
      // Mirrors test_nl_compound_caps_at_max_actions.
      final actions = normalizeActions({
        'intent': 'multi',
        'actions': [for (var i = 0; i < 8; i++) {'intent': 'chat', 'reply': 'reply $i'}],
      });
      expect(actions.map((a) => a['reply']),
          [for (var i = 0; i < nlMaxActions; i++) 'reply $i']);
    });

    test('duplicate delete actions merge meal_indices into the first', () {
      // Mirrors test_nl_compound_duplicate_delete_actions_merge_into_one.
      final actions = normalizeActions({
        'intent': 'multi',
        'actions': [
          {'intent': 'delete', 'meal_indices': [0], 'reason': '第一顿'},
          {'intent': 'chat', 'reply': 'hi'},
          {'intent': 'delete', 'meal_indices': [2], 'reason': '第三顿'},
          {'intent': 'delete', 'meal_indices': 'not-a-list'},
        ],
      });
      final deletes = actions.where((a) => a['intent'] == 'delete');
      expect(deletes, hasLength(1));
      expect(deletes.single['meal_indices'], [0, 2]);
      expect(actions, hasLength(2)); // merged delete + chat
    });
  });

  group('coerceMealIndex (spec §4.3)', () {
    test('hostile index shapes coerce or reject, never throw', () {
      expect(coerceMealIndex(true), isNull);
      expect(coerceMealIndex(false), isNull);
      expect(coerceMealIndex(2), 2);
      expect(coerceMealIndex(2.0), 2);
      expect(coerceMealIndex(2.5), isNull);
      expect(coerceMealIndex(double.nan), isNull);
      expect(coerceMealIndex(double.infinity), isNull);
      expect(coerceMealIndex(' 3 '), 3);
      expect(coerceMealIndex('-1'), -1);
      expect(coerceMealIndex('abc'), isNull);
      expect(coerceMealIndex([1]), isNull);
      expect(coerceMealIndex({'i': 1}), isNull);
      expect(coerceMealIndex(null), isNull);
    });
  });

  group('compound execution (spec §4.1/§4.2/§4.9)', () {
    test('bare-array correction+delete crash regression executes both', () async {
      // Mirrors test_nl_compound_bare_array_crash_regression: the exact
      // 2026-07-16 payload shape for「我是说第二顿饭是烧鸭饭 你重新准确算一下
      // 第二顿饭 删除第三顿饭」.
      final (_, lunchId, dinnerId) = seedThreeMeals();
      final snapshot = await dao.recentMeals();

      final replies = await exec.executeParsed([
        {'intent': 'correction', 'meal_index': 1, 'reason': '第二顿饭改为烧鸭饭', 'analysis': roastDuckAnalysis},
        {'intent': 'delete', 'meal_indices': [2], 'reason': '用户要求删除第三顿饭'},
      ], '我是说第二顿饭是烧鸭饭 你重新准确算一下第二顿饭 删除第三顿饭', snapshot);

      // Lunch corrected in-place by DB id.
      expect(dao.byId(lunchId).analysis['meal_description'], '烧鸭饭');
      expect(dao.byId(lunchId).analysis['total_calories'], 780);
      expect(dao.byId(lunchId).corrected, isTrue);
      // Nothing deleted before the user confirms.
      expect(dao.meals, hasLength(3));
      expect(dao.deletedIds, isEmpty);
      // Correction reply, then the delete confirmation staging.
      expect(replies, hasLength(2));
      expect(replies[0].text, contains('✏️'));
      expect(replies[0].text, contains('烧鸭饭'));
      expect(replies[0].text, contains('(+230)'));
      expect(replies[1].needsDeleteConfirmation, isTrue);
      expect(replies[1].pendingDeleteIds, [dinnerId]);
    });

    test('multi object shape end-to-end through handleText', () async {
      // Mirrors test_nl_compound_multi_object_shape.
      final (_, lunchId, dinnerId) = seedThreeMeals();
      analyzer.next = {
        'intent': 'multi',
        'actions': [
          {'intent': 'correction', 'meal_index': 1, 'reason': '改为烧鸭饭', 'analysis': roastDuckAnalysis},
          {'intent': 'delete', 'meal_indices': [2], 'reason': '删除第三顿'},
        ],
        'reply': '正在更正第二顿并删除第三顿',
      };

      final replies = await exec.handleText('第二顿是烧鸭饭，删除第三顿');

      expect(dao.byId(lunchId).analysis['meal_description'], '烧鸭饭');
      expect(dao.meals, hasLength(3));
      expect(replies.last.needsDeleteConfirmation, isTrue);
      expect(replies.last.pendingDeleteIds, [dinnerId]);
      expect(replies.last.pendingDeleteLabels.single, contains('米饭'));
    });

    test('caps runaway actions at 5 replies', () async {
      analyzer.next = {
        'intent': 'multi',
        'actions': [for (var i = 0; i < 8; i++) {'intent': 'chat', 'reply': 'reply $i'}],
      };
      final replies = await exec.handleText('hello hello');
      expect(replies.map((r) => r.text), [for (var i = 0; i < 5; i++) 'reply $i']);
    });

    test('duplicate deletes stage ONE confirmation holding both meals', () async {
      // Mirrors test_nl_compound_duplicate_delete_actions_merge_into_one.
      final (breakfastId, _, dinnerId) = seedThreeMeals();
      analyzer.next = {
        'intent': 'multi',
        'actions': [
          {'intent': 'delete', 'meal_indices': [0], 'reason': '第一顿'},
          {'intent': 'delete', 'meal_indices': [2], 'reason': '第三顿'},
        ],
      };
      final replies = await exec.handleText('删除第一顿和第三顿');
      final confirms = replies.where((r) => r.needsDeleteConfirmation).toList();
      expect(confirms, hasLength(1));
      expect(confirms.single.pendingDeleteIds..sort(),
          [breakfastId, dinnerId]..sort());
      expect(dao.meals, hasLength(3)); // nothing deleted before confirm
    });

    test('partial failure continues and reports k of N', () async {
      // Mirrors test_nl_compound_partial_failure_continues.
      seedThreeMeals();
      dao.throwOnUpdate = true;
      analyzer.next = {
        'intent': 'multi',
        'actions': [
          {'intent': 'correction', 'meal_index': 0, 'analysis': roastDuckAnalysis},
          {'intent': 'chat', 'reply': 'still here'},
        ],
      };
      final replies = await exec.handleText('fix and chat');
      final texts = replies.map((r) => r.text).toList();
      expect(texts, contains('still here'));
      expect(texts.last,
          '⚠️ 1 of 2 requested action(s) failed — the rest were applied.');
    });

    test('all-failed wording never claims partial success', () async {
      // Mirrors test_nl_compound_all_failed_wording_never_claims_partial_success.
      seedThreeMeals();
      dao.throwOnUpdate = true;
      analyzer.next = {
        'intent': 'multi',
        'actions': [
          {'intent': 'correction', 'meal_index': 0, 'analysis': roastDuckAnalysis},
          {'intent': 'correction', 'meal_index': 1, 'analysis': roastDuckAnalysis},
        ],
      };
      final replies = await exec.handleText('fix them both');
      expect(replies, hasLength(1));
      expect(replies.single.text, contains('All 2 requested actions failed'));
      expect(replies.single.text, isNot(contains('the rest were applied')));
    });

    test('a single failing action uses the singular wording', () async {
      seedThreeMeals();
      dao.throwOnUpdate = true;
      analyzer.next = {'intent': 'correction', 'meal_index': 0, 'analysis': roastDuckAnalysis};
      final replies = await exec.handleText('fix it');
      expect(replies.single.text, '❌ That request failed. Please try again.');
    });

    test('unusable response shapes reply gracefully', () async {
      // Mirrors test_nl_unusable_response_shapes_reply_gracefully.
      for (final payload in ['just a string', 42, <dynamic>[], ['a', 3], null]) {
        final replies = await exec.executeParsed(payload, 'gibberish request', []);
        expect(replies.single.text, contains("couldn't work out what to do"));
      }
    });

    test('unrecognized or non-string intents fall back to chat', () async {
      // Mirrors test_nl_unhashable_or_nonstring_intent_falls_back_to_chat.
      for (final intent in ['muse', [1, 2], {'x': 1}, 7, null]) {
        final replies = await exec.executeParsed(
            {'intent': intent, 'reply': 'fallback reply'}, 'hm', []);
        expect(replies.single.text, 'fallback reply');
      }
    });
  });

  group('correction (spec §4.4)', () {
    test('updates the exact snapshot row by DB id', () async {
      // Mirrors test_correction_by_index_updates_the_exact_five_day_old_row.
      final (_, lunchId, _) = seedThreeMeals();
      analyzer.next = {'intent': 'correction', 'meal_index': 1, 'analysis': roastDuckAnalysis};
      await exec.handleText('第二顿是烧鸭饭');
      expect(dao.updates.single.$1, lunchId);
      expect(dao.byId(lunchId).analysis['total_calories'], 780);
    });

    test('empty database refuses', () async {
      analyzer.next = {'intent': 'correction', 'meal_index': 0, 'analysis': roastDuckAnalysis};
      final replies = await exec.handleText('fix meal 0');
      expect(replies.single.text,
          '❌ Cannot correct because no meals are logged recently.');
    });

    test('hostile index shapes never crash, reply shows the raw value', () async {
      // Mirrors test_nl_correction_hostile_index_never_crashes and
      // test_nl_correction_invalid_index_reply_escapes_model_meal_index.
      seedThreeMeals();
      for (final bad in ['abc', 99, -1, 2.5, true, null, [0], {'i': 0}]) {
        final replies = await exec.executeParsed(
            {'intent': 'correction', 'meal_index': bad, 'analysis': roastDuckAnalysis},
            'fix', await dao.recentMeals());
        expect(replies.single.text, contains('Invalid meal index'));
        expect(replies.single.text, contains('You have 3 recent meals'));
        expect(dao.updates, isEmpty);
      }
    });

    test('invalid-index reply truncates the model value to 40 chars', () async {
      seedThreeMeals();
      final hostile = '<b>${'x' * 100}</b>';
      final replies = await exec.executeParsed(
          {'intent': 'correction', 'meal_index': hostile, 'analysis': roastDuckAnalysis},
          'fix', await dao.recentMeals());
      expect(replies.single.text, contains(hostile.substring(0, 40)));
      expect(replies.single.text, isNot(contains(hostile)));
    });

    test('refuses empty, non-food, or non-map analysis (silent-delete guard)', () async {
      // Mirrors test_nl_correction_refuses_empty_or_nonfood_analysis.
      final (_, lunchId, _) = seedThreeMeals();
      for (final bad in [
        <String, dynamic>{},
        {'is_food': false, 'meal_description': 'x'},
        null,
        'not a map',
      ]) {
        final replies = await exec.executeParsed(
            {'intent': 'correction', 'meal_index': 1, 'analysis': bad},
            '改一下第二顿', await dao.recentMeals());
        expect(replies.single.text, contains('left the meal unchanged'));
      }
      expect(dao.updates, isEmpty);
      expect(dao.byId(lunchId).analysis['meal_description'], '面条');
      expect(dao.byId(lunchId).analysis['total_calories'], 550);
    });

    test('string calories: diff computed via safeNumber BEFORE the write', () async {
      // Mirrors test_correction_reply_escapes_hostile_meal_and_gemini_text:
      // a hallucinated string calorie must not throw after the row was
      // rewritten; safeNumber coerces it to 0 for the reply arithmetic.
      final (_, lunchId, _) = seedThreeMeals();
      final replies = await exec.executeParsed({
        'intent': 'correction',
        'meal_index': 1,
        'analysis': {'is_food': true, 'meal_description': '烧鸭饭', 'total_calories': '七百八'},
        'reason': '<img src=x onerror=alert(1)>',
      }, 'fix that meal', await dao.recentMeals());
      expect(dao.updates.single.$1, lunchId); // the write DID happen
      expect(replies.single.text, contains('🔥 550 kcal → 0 kcal (-550)'));
      expect(replies.single.text, contains('<img src=x onerror=alert(1)>'));
    });

    test('sanitizes hostile food_items before persisting', () async {
      seedThreeMeals();
      await exec.executeParsed({
        'intent': 'correction',
        'meal_index': 0,
        'analysis': {
          'is_food': true,
          'meal_description': 'fixed',
          'total_calories': 200,
          'food_items': ['scalar', {'name': 'kept'}, 7],
        },
      }, 'fix', await dao.recentMeals());
      expect(dao.updates.single.$2['food_items'], [{'name': 'kept'}]);
    });

    test('reply shape includes diff and reason', () async {
      seedThreeMeals();
      final replies = await exec.executeParsed(
          {'intent': 'correction', 'meal_index': 1, 'analysis': roastDuckAnalysis, 'reason': '改为烧鸭饭'},
          'fix', await dao.recentMeals());
      expect(replies.single.text, '✏️ Corrected meal 2!\n\n'
          '面条 → 烧鸭饭\n🔥 550 kcal → 780 kcal (+230)\n\n💬 改为烧鸭饭');
    });
  });

  group('delete (spec §4.5)', () {
    test('requires confirmation, then confirmPendingDelete deletes', () async {
      // Mirrors test_b7_nl_delete_requires_confirmation_then_deletes.
      final (breakfastId, _, _) = seedThreeMeals();
      analyzer.next = {
        'intent': 'delete',
        'meal_indices': [0],
        'reason': 'user asked',
      };
      final replies = await exec.handleText('delete the porridge');
      final r = replies.single;
      expect(r.needsDeleteConfirmation, isTrue);
      expect(r.pendingDeleteIds, [breakfastId]);
      expect(r.pendingDeleteLabels.single, '白粥 (2026-07-17 08:00 AM, ~150 kcal)');
      expect(r.text, contains('Delete 1 meal(s)?'));
      expect(r.text, contains('This cannot be undone.'));
      expect(dao.meals, hasLength(3)); // NOTHING deleted yet

      final confirmText = await exec.confirmPendingDelete(r.pendingDeleteIds);
      expect(dao.deletedIds, [breakfastId]);
      expect(dao.meals, hasLength(2));
      expect(confirmText, contains('Deleted 1 meal(s)'));
      expect(confirmText, contains('白粥'));
    });

    test('cancel (never confirming) keeps every meal', () async {
      // Mirrors test_nl_delete_cancel_keeps_meals: the modal dialog's Cancel
      // is simply "confirmPendingDelete never called".
      seedThreeMeals();
      analyzer.next = {'intent': 'delete', 'meal_indices': [0, 1, 2]};
      await exec.handleText('delete everything');
      expect(dao.meals, hasLength(3));
      expect(dao.deletedIds, isEmpty);
    });

    test('empty database refuses', () async {
      // Mirrors test_b12_delete_intent_with_empty_database.
      analyzer.next = {'intent': 'delete', 'meal_indices': [0]};
      final replies = await exec.handleText('delete it');
      expect(replies.single.text,
          '❌ Cannot delete because no meals are logged recently.');
    });

    test('all-invalid indices never crash or stash', () async {
      // Mirrors test_nl_delete_all_invalid_indices_never_crash_or_stash.
      seedThreeMeals();
      final replies = await exec.executeParsed(
          {'intent': 'delete', 'meal_indices': ['abc', null, true, 9.5, 99, -1]},
          'delete', await dao.recentMeals());
      expect(replies.single.needsDeleteConfirmation, isFalse);
      expect(replies.single.text, "❌ Couldn't match those meals to the recent list.");
      expect(dao.deletedIds, isEmpty);
    });

    test('mixed indices honor only the valid in-range ones', () async {
      // Mirrors test_nl_delete_mixed_indices_honors_only_the_valid_ones.
      final (breakfastId, lunchId, _) = seedThreeMeals();
      final replies = await exec.executeParsed(
          {'intent': 'delete', 'meal_indices': [0, '1', 'x', null, true, 7.5, -1, 99, 0]},
          'delete', await dao.recentMeals());
      expect(replies.single.pendingDeleteIds, [breakfastId, lunchId]);
    });

    test('scalar meal_indices: bare int honored, string "12" is NOT split per char', () async {
      final (_, lunchId, _) = seedThreeMeals();
      final honored = await exec.executeParsed(
          {'intent': 'delete', 'meal_indices': 1}, 'delete', await dao.recentMeals());
      expect(honored.single.pendingDeleteIds, [lunchId]);

      final refused = await exec.executeParsed(
          {'intent': 'delete', 'meal_indices': '12'}, 'delete', await dao.recentMeals());
      expect(refused.single.needsDeleteConfirmation, isFalse);
      expect(refused.single.text, contains("Didn't catch which meals to delete"));
    });

    test('missing indices ask for specifics', () async {
      seedThreeMeals();
      final replies = await exec.executeParsed(
          {'intent': 'delete'}, 'delete', await dao.recentMeals());
      expect(replies.single.text, contains("Didn't catch which meals to delete"));
    });
  });

  group('new_meal (spec §4.6)', () {
    test('is_food gate: saves with source manual_text', () async {
      analyzer.next = {'intent': 'new_meal', 'analysis': roastDuckAnalysis, 'reply': 'ok'};
      final replies = await exec.handleText('I ate roast duck rice');
      final saved = dao.savedMeals.single;
      expect(saved.source, 'manual_text');
      expect(saved.imageHash, '');
      expect(saved.analysis['meal_description'], '烧鸭饭');
      expect(replies.single.text, startsWith('✅ Added new manual meal:'));
      expect(replies.single.text, contains('~780 kcal'));
    });

    test('non-food or missing analysis refuses', () async {
      for (final bad in [
        null,
        <String, dynamic>{},
        {'is_food': false},
        'nonsense',
      ]) {
        final replies = await exec.executeParsed(
            {'intent': 'new_meal', 'analysis': bad}, 'hm', []);
        expect(replies.single.text, "🚫 I couldn't detect food in that description.");
      }
      expect(dao.savedMeals, isEmpty);
    });

    test('hostile food_items sanitized before save', () async {
      await exec.executeParsed({
        'intent': 'new_meal',
        'analysis': {
          'is_food': true,
          'meal_description': 'soup',
          'total_calories': 90,
          'food_items': 'not-a-list',
        },
      }, 'soup', []);
      expect(dao.savedMeals.single.analysis['food_items'], isEmpty);
    });
  });

  group('log_weight (spec §4.7)', () {
    test('parses kg from the raw text and saves', () async {
      // Mirrors test_nl_log_weight_saves.
      analyzer.next = {'intent': 'log_weight', 'weight_kg': 0, 'reply': 'ok'};
      final replies = await exec.handleText('I weigh 72.5 kg this morning');
      expect(dao.savedWeights.single.$2, 72.5);
      expect(replies.single.text, contains('Logged 72.5 kg for'));
    });

    test('pounds convert to kg', () async {
      analyzer.next = {'intent': 'log_weight', 'reply': 'ok'};
      await exec.handleText('weighed 159 lb today');
      expect(dao.savedWeights.single.$2, 72.1);
    });

    test('hostile weight_kg fields save nothing', () async {
      // Mirrors test_nl_log_weight_hostile_field_saves_nothing — string
      // numerics are deliberately NOT trusted.
      for (final bad in ['72.5', [72.5], -50, double.infinity, null]) {
        analyzer.next = {'intent': 'log_weight', 'weight_kg': bad, 'reply': 'ok'};
        final replies = await exec.handleText('please log my weight');
        expect(replies.single.text, contains("couldn't read a valid body weight"));
      }
      expect(dao.savedWeights, isEmpty);
    });

    test('bounded numeric field is the fallback that saves', () async {
      // Mirrors test_nl_log_weight_numeric_field_fallback_saves.
      analyzer.next = {'intent': 'log_weight', 'weight_kg': 72.5, 'reply': 'ok'};
      await exec.handleText('log my usual morning weight');
      expect(dao.savedWeights.single.$2, 72.5);
    });

    test('kilometers and scientific notation are not weights', () {
      expect(parseWeightKg('ran 42 kilometers today'), isNull);
      expect(parseWeightKg('the dataset weighs 1e72 units'), isNull);
      expect(parseWeightKg('I weigh 72.5kg'), 72.5);
      expect(parseWeightKg('weight: 500 kg'), isNull); // out of 30..300
      expect(parseWeightKg(null), isNull);
    });
  });

  group('log_activity (spec §4.8)', () {
    test('saves and formats the non-zero parts', () async {
      // Mirrors test_nl_log_activity_saves.
      analyzer.next = {
        'intent': 'log_activity',
        'active_calories': 450,
        'steps': 8000,
        'distance_km': 5,
        'reply': 'ok',
      };
      final replies = await exec.handleText('burned 450 on my 5 km run, 8000 steps');
      final row = dao.savedActivities.single;
      expect(row['active_calories'], 450);
      expect(row['steps'], 8000);
      expect(row['distance_km'], 5.0);
      expect(replies.single.text, contains('450 kcal · 8,000 steps · 5 km'));
    });

    test('hostile payloads save nothing', () async {
      // Mirrors test_nl_log_activity_hostile_payload_saves_nothing.
      for (final payload in [
        {'active_calories': double.infinity, 'steps': 'many', 'distance_km': -3},
        {'active_calories': double.nan, 'steps': -5, 'distance_km': 'far'},
        {'active_calories': null, 'steps': null, 'distance_km': null},
        {'active_calories': [450], 'steps': {'n': 1}, 'distance_km': false},
      ]) {
        analyzer.next = {'intent': 'log_activity', 'reply': 'ok', ...payload};
        final replies = await exec.handleText('log my workout from earlier');
        expect(replies.single.text, contains("couldn't find any activity numbers"));
      }
      expect(dao.savedActivities, isEmpty);
    });

    test('keeps valid fields and drops junk', () async {
      // Mirrors test_nl_log_activity_keeps_valid_fields_and_drops_junk.
      analyzer.next = {
        'intent': 'log_activity',
        'active_calories': 450,
        'steps': 'many',
        'distance_km': -3,
        'reply': 'ok',
      };
      final replies = await exec.handleText('gym session done');
      final row = dao.savedActivities.single;
      expect(row['active_calories'], 450);
      expect(row['steps'], isNull);
      expect(row['distance_km'], isNull);
      expect(replies.single.text, contains('450 kcal'));
      expect(replies.single.text.split('(').first, isNot(contains('km')));
    });
  });

  group('chat fallback + prompt build (spec §1.2/§4.9)', () {
    test('blank or non-string reply falls back to the default text', () async {
      for (final bad in [null, '', '   ', 42, ['x']]) {
        final replies =
            await exec.executeParsed({'intent': 'chat', 'reply': bad}, 'hm', []);
        expect(replies.single.text,
            "I'm not sure what you mean. Try describing a meal or correction!");
      }
    });

    test('prompt injects relative date context and server-format meal lines', () async {
      // Mirrors test_text_handler_injects_relative_date_context.
      final now = DateTime.now();
      final today = '${now.year.toString().padLeft(4, '0')}-'
          '${now.month.toString().padLeft(2, '0')}-'
          '${now.day.toString().padLeft(2, '0')}';
      final y = DateTime(now.year, now.month, now.day - 1);
      final yesterday = '${y.year.toString().padLeft(4, '0')}-'
          '${y.month.toString().padLeft(2, '0')}-'
          '${y.day.toString().padLeft(2, '0')}';
      const weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

      dao.seed('白粥', 150);
      analyzer.next = {'intent': 'chat', 'reply': 'hi'};
      await exec.handleText('what did I eat yesterday?');

      expect(analyzer.lastPrompt,
          contains('today is $today (${weekdays[now.weekday - 1]}); yesterday was $yesterday'));
      expect(analyzer.lastPrompt,
          contains('[0] Date: 2026-07-17 | Meal: 白粥 (~150 kcal) — Items: '));
      expect(analyzer.lastPrompt, contains('The user says: "what did I eat yesterday?"'));
      expect(dao.lastRecentDays, textEditWindowDays);
    });

    test('empty snapshot renders the no-meals sentinel line', () async {
      analyzer.next = {'intent': 'chat', 'reply': 'hi'};
      await exec.handleText('hello');
      expect(analyzer.lastPrompt, contains('No meals logged recently.'));
    });

    test('poison stored analysis cannot break prompt building', () async {
      dao.seed('x', 0, analysis: {
        'is_food': true,
        'food_items': ['scalar', 42, {'name': null}],
        // meal_description and total_calories missing entirely
      });
      analyzer.next = {'intent': 'chat', 'reply': 'hi'};
      final replies = await exec.handleText('hello');
      expect(replies.single.text, 'hi');
      expect(analyzer.lastPrompt,
          contains('[0] Date: 2026-07-17 | Meal: Unknown (~0 kcal) — Items: ?'));
    });
  });

  group('safety wrappers (spec §3.3/§4.9)', () {
    test('null from the analyzer reports an AI-contact error', () async {
      analyzer.next = null;
      final replies = await exec.handleText('anything');
      expect(replies.single.text, '❌ Error contacting AI. Please try again.');
    });

    test('a throwing analyzer reports an AI-contact error', () async {
      analyzer.throwOnText = true;
      final replies = await exec.handleText('anything');
      expect(replies.single.text, '❌ Error contacting AI. Please try again.');
    });

    test('handleText contains ANY crash (safe-dispatcher parity)', () async {
      // Mirrors test_handle_text_message_safe_contains_any_crash.
      final exploding = _ExplodingDao();
      final safeExec =
          createExecutor(exploding, analyzer, settings) as DefaultNlExecutor;
      final replies = await safeExec.handleText('anything');
      expect(replies.single.text,
          '❌ Something went wrong handling that message. Please try again.');
    });

    test('quota pause refuses without a model call', () async {
      await settings
          .setQuotaPauseUntil(DateTime.now().add(const Duration(hours: 6)));
      final replies = await exec.handleText('log my lunch');
      expect(replies.single.text, contains('Gemini is paused right now.'));
      expect(replies.single.text, contains('did not send this request'));
      expect(analyzer.lastPrompt, isNull); // NO model call during the pause
    });
  });
}

class _ExplodingDao extends FakeDao {
  @override
  Future<List<Meal>> recentMeals({int days = 7}) async =>
      throw StateError('db exploded');
}
