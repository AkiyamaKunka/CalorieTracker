// describeMeal (spec §9 app-only): free text → a NEW-meal analysis for
// PREVIEW. The guarantees that matter:
//   * the prompt carries an EMPTY meals list, so the shared prompt's own
//     rule makes the intent deterministic — a description can never be
//     applied as a correction to an existing meal;
//   * nothing is saved (the editor does that after the user looks);
//   * every hostile reply shape degrades to a message, never a throw.
import 'package:calorie_tracker/services/nl/executor.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

Map<String, dynamic> foodAnalysis({num cal = 650}) => {
      'is_food': true,
      'meal_description': 'Beef noodle soup and two eggs',
      'total_calories': cal,
      'total_protein_g': 35,
      'total_carbs_g': 78,
      'total_fat_g': 20,
      'food_items': [
        {'name': 'Beef noodle soup', 'estimated_calories': 520},
        {'name': 'Boiled egg', 'estimated_calories': 130},
      ],
    };

void main() {
  late FakeDao dao;
  late FakeAnalyzer analyzer;
  late DefaultNlExecutor executor;

  setUp(() async {
    dao = FakeDao();
    analyzer = FakeAnalyzer();
    executor = DefaultNlExecutor(dao, analyzer, await testSettings());
  });

  test('returns the analysis WITHOUT saving anything', () async {
    analyzer.next = {'intent': 'new_meal', 'analysis': foodAnalysis()};
    final out = await executor.describeMeal('beef noodle soup and two eggs');
    expect(out.ok, isTrue);
    expect(out.analysis!['total_calories'], 650);
    expect(dao.savedMeals, isEmpty, reason: 'preview must not persist');
    expect(dao.meals, isEmpty);
  });

  test('the prompt carries an EMPTY meals list — never a correction target',
      () async {
    // Seed real meals: handleText would offer these as correction targets.
    dao.seed('Existing lunch', 700);
    dao.seed('Existing dinner', 900);
    analyzer.next = {'intent': 'new_meal', 'analysis': foodAnalysis()};
    await executor.describeMeal('a latte');
    final prompt = analyzer.lastPrompt!;
    expect(prompt, contains('a latte'));
    expect(prompt, isNot(contains('Existing lunch')),
        reason: 'a described meal must not be matchable to an existing one');
    expect(prompt, isNot(contains('Existing dinner')));
  });

  test('works with non-English input (prompt is multilingual)', () async {
    analyzer.next = {'intent': 'new_meal', 'analysis': foodAnalysis(cal: 480)};
    final out = await executor.describeMeal('一碗牛肉面加一个鸡蛋');
    expect(out.ok, isTrue);
    expect(analyzer.lastPrompt, contains('一碗牛肉面加一个鸡蛋'));
    expect(out.analysis!['total_calories'], 480);
  });

  test('food_items are sanitized before preview (§3.5)', () async {
    analyzer.next = {
      'intent': 'new_meal',
      'analysis': {
        'is_food': true,
        'total_calories': 100,
        'food_items': [
          {'name': 'ok'},
          'not a map',
          42,
        ],
      },
    };
    final out = await executor.describeMeal('something');
    expect(out.analysis!['food_items'], hasLength(1));
  });

  test('a bare-array reply still yields the new meal (§4.1 normalization)',
      () async {
    analyzer.next = {
      'actions': [
        {'intent': 'chat', 'message': 'hmm'},
        {'intent': 'new_meal', 'analysis': foodAnalysis(cal: 300)},
      ],
    };
    final out = await executor.describeMeal('two eggs');
    expect(out.analysis!['total_calories'], 300);
  });

  test('non-food, empty text, and unusable replies give messages not throws',
      () async {
    expect((await executor.describeMeal('   ')).error, contains('what you ate'));

    analyzer.next = {
      'intent': 'new_meal',
      'analysis': {'is_food': false}
    };
    expect((await executor.describeMeal('a rock')).error,
        contains("couldn't detect food"));

    analyzer.next = {'intent': 'chat', 'message': 'hello'};
    expect((await executor.describeMeal('hi')).ok, isFalse);

    analyzer.next = null; // analyzer folds transport/parse failures to null
    expect((await executor.describeMeal('eggs')).error, contains('Error'));

    analyzer.throwOnText = true; // belt and braces: never propagates
    expect((await executor.describeMeal('eggs')).error, contains('Error'));
  });

  test('multiple described meals: first previewed, the rest ANNOUNCED',
      () async {
    analyzer.next = {
      'intent': 'multi',
      'actions': [
        {'intent': 'new_meal', 'analysis': foodAnalysis(cal: 400)},
        {'intent': 'new_meal', 'analysis': foodAnalysis(cal: 250)},
      ],
    };
    final out = await executor.describeMeal(
        'eggs for breakfast, then a salad for lunch');
    expect(out.analysis!['total_calories'], 400);
    expect(out.warning, contains('2 meals'),
        reason: 'dropping the rest silently under-counts the day');
  });

  test('a single meal carries no warning', () async {
    analyzer.next = {'intent': 'new_meal', 'analysis': foodAnalysis()};
    expect((await executor.describeMeal('eggs')).warning, isNull);
  });

  test('a quota pause refuses BEFORE any model call', () async {
    final settings = await testSettings();
    await settings.setQuotaPauseUntil(
        DateTime.now().add(const Duration(hours: 3)));
    final paused = DefaultNlExecutor(dao, analyzer, settings);
    final out = await paused.describeMeal('eggs');
    expect(out.ok, isFalse);
    expect(analyzer.lastPrompt, isNull, reason: 'no model call while paused');
  });
}
