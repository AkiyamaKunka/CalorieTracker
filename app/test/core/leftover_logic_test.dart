// The leftover deduction MATH (user feature 2026-08-04). The model only
// supplies fractions; these pin that every stored number is computed,
// clamped, and re-basable here — and that hostile model output can never
// inflate a meal or crash the flow.
import 'dart:convert';

import 'package:calorie_tracker/core/leftover_logic.dart';
import 'package:calorie_tracker/core/shared_generated.dart';
import 'package:flutter_test/flutter_test.dart';

Map<String, dynamic> meal() => {
      'is_food': true,
      'meal_description': 'Cafeteria tray',
      'total_calories': 965,
      'total_protein_g': 46,
      'total_carbs_g': 90,
      'total_fat_g': 47,
      'food_items': [
        {'name': 'Mapo tofu (~200 g)', 'estimated_calories': 285},
        {'name': 'Braised pork ribs (~150 g)', 'estimated_calories': 380},
        {'name': 'Steamed rice (~200 g)', 'estimated_calories': 300},
      ],
    };

void main() {
  test('per-item fractions deduct deterministically', () {
    final r = applyLeftover(
        meal(),
        {
          'same_meal': true,
          'confidence': 0.9,
          'leftover_fraction': 0.3,
          'items': [
            {'name': 'Mapo tofu (~200 g)', 'left_fraction': 0.5},
            {'name': 'Braised pork ribs (~150 g)', 'left_fraction': 0.0},
            {'name': 'Steamed rice (~200 g)', 'left_fraction': 1.0},
          ],
        },
        leftoverPhotoMd5: 'aa',
        appliedAtIso: '2026-08-05T12:00:00')!;
    // eaten = 285*0.5 + 380*1.0 + 300*0.0 = 142.5+380 = 522.5 of 965
    expect(r.adjusted['total_calories'], 523);
    expect(r.deductedKcal, 965 - 523);
    expect(r.sameMeal, isTrue);
    // Macros scale by the SAME eaten fraction as the calories.
    final frac = 522.5 / 965;
    expect(r.adjusted['total_protein_g'],
        closeTo(46 * frac, 0.06));
    // The original is stashed for re-application.
    expect(r.adjusted['leftover']['original']['total_calories'], 965);
  });

  test('re-application bases on the ORIGINAL, never compounds', () {
    final first = applyLeftover(meal(), {'leftover_fraction': 0.5},
        leftoverPhotoMd5: 'aa', appliedAtIso: 't1')!;
    expect(first.adjusted['total_calories'], 483);
    // Later the user eats more of it: only 10% left now.
    final second = applyLeftover(
        first.adjusted, {'leftover_fraction': 0.1},
        leftoverPhotoMd5: 'bb', appliedAtIso: 't2')!;
    expect(second.adjusted['total_calories'], (965 * 0.9).round(),
        reason: '10% of the ORIGINAL is left → 90% of 965, not 90% of 483');
    expect(second.adjusted['leftover']['original']['total_calories'], 965);
  });

  test('hostile fractions clamp; calories can never grow', () {
    final r = applyLeftover(
        meal(),
        {
          'leftover_fraction': -3,
          'items': [
            {'name': 'Mapo tofu (~200 g)', 'left_fraction': 250},
            {'name': 'Braised pork ribs (~150 g)', 'left_fraction': double.nan},
          ],
        },
        leftoverPhotoMd5: 'aa',
        appliedAtIso: 't')!;
    final total = r.adjusted['total_calories'] as int;
    expect(total, inInclusiveRange(0, 965));
    expect(r.deductedKcal, inInclusiveRange(0, 965));
  });

  test('a reply with no usable fraction returns null (never guesses)', () {
    expect(
        applyLeftover(meal(), {'note': 'nice plate'},
            leftoverPhotoMd5: 'aa', appliedAtIso: 't'),
        isNull);
    expect(
        applyLeftover(meal(), {'leftover_fraction': 'lots'},
            leftoverPhotoMd5: 'aa', appliedAtIso: 't'),
        isNull);
  });

  test('itemless meals fall back to the overall fraction', () {
    final m = meal()..remove('food_items');
    final r = applyLeftover(m, {'leftover_fraction': 0.25},
        leftoverPhotoMd5: 'aa', appliedAtIso: 't')!;
    expect(r.adjusted['total_calories'], (965 * 0.75).round());
  });

  test('same_meal false surfaces; absent defaults true', () {
    final no = applyLeftover(meal(), {'same_meal': false, 'leftover_fraction': 0.5},
        leftoverPhotoMd5: 'aa', appliedAtIso: 't')!;
    expect(no.sameMeal, isFalse);
    final absent = applyLeftover(meal(), {'leftover_fraction': 0.5},
        leftoverPhotoMd5: 'aa', appliedAtIso: 't')!;
    expect(absent.sameMeal, isTrue);
  });

  test('compactOriginalAnalysis whitelists, caps, and stays parseable', () {
    final huge = meal();
    huge['secret_blob'] = 'x' * 10000;
    (huge['food_items'] as List).add(
        {'name': 'y' * 6000, 'estimated_calories': 10, 'junk': 'z' * 6000});
    final out = compactOriginalAnalysis(huge);
    expect(out.length, lessThanOrEqualTo(SharedConstants.apiLeftoverMaxChars));
    final parsed = jsonDecode(out) as Map<String, dynamic>;
    expect(parsed.containsKey('secret_blob'), isFalse,
        reason: 'only whitelisted fields ride into the prompt');
    expect(parsed['total_calories'], 965);
  });

  test('compact uses the pre-deduction original after an application', () {
    final applied = applyLeftover(meal(), {'leftover_fraction': 0.5},
        leftoverPhotoMd5: 'aa', appliedAtIso: 't')!;
    final compact =
        jsonDecode(compactOriginalAnalysis(applied.adjusted)) as Map;
    expect(compact['total_calories'], 965,
        reason: 're-analysis must describe the ORIGINAL portions');
  });
}
