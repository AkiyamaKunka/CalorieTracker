// Editor rules (spec §9 app-only divergence: hand-edited meals). Field
// parsing/bounds, item sums, and the analysis merge that decides what is
// persisted — the merge is where a bug silently corrupts a meal row.
import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/meal_edit_logic.dart';
import 'package:flutter_test/flutter_test.dart';

Meal mealWith(Map<String, dynamic> analysis,
        {String date = '2026-07-24', String time = '01:30 PM'}) =>
    Meal(
      id: 7,
      date: date,
      time: time,
      timestamp: '2026-07-24T13:30:00.000',
      source: 'app_photo',
      imageHash: 'a' * 32,
      analysis: analysis,
    );

void main() {
  group('parseNumberField', () {
    test('blank is null with no error (caller treats as 0)', () {
      final p = parseNumberField('  ', label: 'Calories', max: 100);
      expect(p.value, isNull);
      expect(p.ok, isTrue);
    });

    test('accepts integers and decimals', () {
      expect(parseNumberField('450', label: 'x', max: 1000).value, 450);
      expect(parseNumberField(' 12.5 ', label: 'x', max: 1000).value, 12.5);
    });

    test('rejects text, negatives, and over-max with a named message', () {
      expect(parseNumberField('abc', label: 'Protein', max: 100).error,
          contains('Protein'));
      expect(parseNumberField('-1', label: 'Protein', max: 100).error,
          contains('negative'));
      expect(parseNumberField('101', label: 'Protein', max: 100).error,
          contains('too large'));
    });

    test('rejects NaN/Infinity spellings', () {
      for (final s in ['NaN', 'Infinity', '-Infinity']) {
        expect(parseNumberField(s, label: 'x', max: 1e6).ok, isFalse,
            reason: '$s must not reach the coercion layer');
      }
    });
  });

  group('clock round-trip', () {
    test('formats and parses the stored 12-hour format', () {
      expect(formatClock(DateTime(2026, 7, 24, 13, 5)), '01:05 PM');
      expect(formatClock(DateTime(2026, 7, 24, 0, 30)), '12:30 AM');
      expect(formatClock(DateTime(2026, 7, 24, 12, 0)), '12:00 PM');
      expect(parseClock('01:05 PM'), (hour: 13, minute: 5));
      expect(parseClock('12:30 AM'), (hour: 0, minute: 30));
      expect(parseClock('12:00 PM'), (hour: 12, minute: 0));
    });

    test('rejects junk', () {
      for (final s in ['', '25:00 PM', '13:00 PM', '07:60 AM', 'noon']) {
        expect(parseClock(s), isNull, reason: s);
      }
    });
  });

  group('MealDraft.fromMeal', () {
    test('loads totals, items and leaves an unset description blank', () {
      final draft = MealDraft.fromMeal(mealWith({
        'is_food': true,
        'total_calories': 450,
        'total_protein_g': 50,
        'total_carbs_g': 12,
        'total_fat_g': 22.5,
        'food_items': [
          {'name': 'Chicken', 'estimated_calories': 280, 'protein_g': 43},
        ],
      }));
      expect(draft.description, ''); // NOT the display fallback 'Meal'
      expect(draft.calories, '450');
      expect(draft.fat, '22.5');
      expect(draft.items.single.name, 'Chicken');
      expect(draft.items.single.carbs, ''); // absent → blank, not '0'
    });

    test('survives hostile analysis shapes', () {
      final draft = MealDraft.fromMeal(mealWith({
        'total_calories': 'lots',
        'total_protein_g': double.infinity,
        'food_items': 'not a list',
        'meal_description': 42,
      }));
      expect(draft.calories, '');
      expect(draft.protein, '');
      expect(draft.items, isEmpty);
      expect(draft.description, '');
    });
  });

  group('validate', () {
    MealDraft good() => MealDraft(
          description: 'Lunch',
          dateIso: '2026-07-24',
          time: '01:30 PM',
          calories: '500',
          protein: '30',
          carbs: '40',
          fat: '10',
          items: [],
        );

    test('a clean draft has no errors', () {
      expect(good().validate(), isEmpty);
    });

    test('catches bad dates, times, and fields', () {
      final d = good()
        ..dateIso = '2026-13-40'
        ..time = '99:99'
        ..calories = '-5';
      final errors = d.validate();
      expect(errors.any((e) => e.contains('Date')), isTrue);
      expect(errors.any((e) => e.contains('Time')), isTrue);
      expect(errors.any((e) => e.contains('Calories')), isTrue);
    });

    test('rejects dates that do not exist (Dart parse rolls them over)', () {
      for (final bad in ['2026-13-40', '2026-02-30', '2026-00-10', '26-07-24']) {
        expect((good()..dateIso = bad).validate(), isNotEmpty, reason: bad);
      }
      expect((good()..dateIso = '2024-02-29').validate(), isEmpty,
          reason: 'a real leap day must be accepted');
    });

    test('blank item rows are ignored, filled ones are validated', () {
      final d = good()
        ..items = [
          MealItemDraft(), // untouched row the user added and left alone
          MealItemDraft(name: 'Rice', calories: 'abc'),
        ];
      final errors = d.validate();
      expect(errors, hasLength(1));
      expect(errors.single, contains('Item 1 calories'));
    });
  });

  group('toAnalysis', () {
    test('preserves unknown keys and forces is_food true', () {
      final draft = MealDraft.fromMeal(mealWith({
        'is_food': false, // e.g. a mis-detected photo the user is fixing
        'analyzed_by': 'claude',
        'total_calories': 0,
      }))
        ..description = 'Congee'
        ..calories = '320'
        ..protein = '9';
      final out = draft.toAnalysis();
      expect(out['analyzed_by'], 'claude'); // untouched passenger key
      expect(out['is_food'], isTrue); // else every read path hides the meal
      expect(out['meal_description'], 'Congee');
      expect(out['total_calories'], 320);
      expect(out['total_protein_g'], 9);
      expect(out['total_carbs_g'], 0); // blank means zero for a curated meal
      expect(out['food_items'], isEmpty);
    });

    test('empty description falls back to the display default', () {
      final out = (MealDraft.blank(DateTime(2026, 7, 24))..calories = '10')
          .toAnalysis();
      expect(out['meal_description'], 'Meal');
    });

    test('items are normalized with numeric fields', () {
      final out = (MealDraft.blank(DateTime(2026, 7, 24))
            ..items = [
              MealItemDraft(name: ' Toast ', calories: '90', fat: '2'),
              MealItemDraft(), // dropped
            ])
          .toAnalysis();
      final items = out['food_items'] as List;
      expect(items, hasLength(1));
      expect(items.single, {
        'name': 'Toast',
        'estimated_calories': 90,
        'protein_g': 0,
        'carbs_g': 0,
        'fat_g': 2,
      });
    });
  });

  test('itemTotals sums only filled rows', () {
    final d = MealDraft.blank(DateTime(2026, 7, 24))
      ..items = [
        MealItemDraft(name: 'A', calories: '100', protein: '10'),
        MealItemDraft(name: 'B', calories: '50.5', fat: '3'),
        MealItemDraft(),
      ];
    final t = d.itemTotals();
    expect(t.calories, 150.5);
    expect(t.protein, 10);
    expect(t.fat, 3);
    expect(t.carbs, 0);
  });
}
