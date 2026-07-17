/// Pinning tests for the report builders (docs/APP_PORT_SPEC.md §5).
///
/// These mirror the Python formatter behaviors: safeNumber clamp rules over
/// hostile stored analyses, the 7-day-median typical-day rule, the history
/// negative clamp + truncated average, the daily report's mismatch and
/// duplicate flags with their exact gating, the 0<cal<1e9 7-day-average
/// filter, the weight section, and get_meal_stats truthiness semantics.
library;

import 'dart:convert';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/report/builders.dart';
import 'package:flutter_test/flutter_test.dart';

class FakeMealsDao implements MealsDao {
  final List<Meal> meals = [];
  final List<Map<String, dynamic>> activities = [];
  final List<Map<String, dynamic>> weights = [];

  @override
  Future<List<Meal>> mealsBetween(String startDate, String endDate) async =>
      meals
          .where((m) =>
              m.date.compareTo(startDate) >= 0 && m.date.compareTo(endDate) <= 0)
          .toList();

  @override
  Future<List<Meal>> recentMeals({int days = 7}) async => meals;

  @override
  Future<String> exportJson() async => jsonEncode({
        'meals': <Object>[],
        'body_weight': weights,
        'activities': activities,
      });

  @override
  Future<int> saveMeal(Meal meal, {IngestionStatus? markStatus}) async {
    meals.add(meal);
    return meals.length;
  }

  @override
  Future<void> updateMealAnalysis(
      int mealId, Map<String, dynamic> analysis) async {}

  @override
  Future<void> deleteMeal(int mealId) async {}

  @override
  Future<bool> isDuplicatePhoto(String imageHash) async => false;

  @override
  Future<bool> reservePhotoHash(String imageHash,
          {required String source, bool reclaimDeliberate = false}) async =>
      true;

  @override
  Future<void> markPhotoHash(String imageHash, IngestionStatus status,
      {int? mealId}) async {}

  @override
  Future<void> releasePhotoHash(String imageHash) async {}

  @override
  Future<void> reclaimStaleProcessing() async {}

  @override
  Future<void> saveBodyWeight(String date, double kg) async {}

  @override
  Future<void> saveActivity(String date,
      {num? activeCalories, int? steps, double? distanceKm}) async {}
}

int _nextId = 1;

Meal meal({
  required String date,
  String time = '12:00 PM',
  Map<String, dynamic>? analysis,
  bool corrected = false,
  String imageHash = '',
  String source = 'camera',
}) =>
    Meal(
      id: _nextId++,
      date: date,
      time: time,
      timestamp: '${date}T12:00:00.000',
      source: source,
      imageHash: imageHash,
      analysis: analysis ?? const {},
      corrected: corrected,
    );

Map<String, dynamic> food(num? cal,
        {dynamic isFood = true, num p = 0, num c = 0, num f = 0}) =>
    {
      'is_food': isFood,
      'total_calories': cal,
      'total_protein_g': p,
      'total_carbs_g': c,
      'total_fat_g': f,
    };

void main() {
  // Fixed clock: Friday 2026-07-17, 20:00 local.
  final clock = DateTime(2026, 7, 17, 20, 0);
  late FakeMealsDao dao;
  late ReportBuilderImpl builder;

  setUp(() {
    dao = FakeMealsDao();
    builder = ReportBuilderImpl(dao, clock: () => clock);
  });

  group('coercion helpers (spec §3.5)', () {
    test('isFoodTruthy reproduces Python truthiness across JSON shapes', () {
      expect(isFoodTruthy(true), isTrue);
      expect(isFoodTruthy(1), isTrue);
      expect(isFoodTruthy('yes'), isTrue);
      expect(isFoodTruthy('0'), isTrue); // non-empty string is truthy
      expect(isFoodTruthy([1]), isTrue);
      expect(isFoodTruthy({'k': 1}), isTrue);
      expect(isFoodTruthy(double.nan), isTrue); // Python bool(nan) is True
      expect(isFoodTruthy(false), isFalse);
      expect(isFoodTruthy(0), isFalse);
      expect(isFoodTruthy(0.0), isFalse);
      expect(isFoodTruthy(''), isFalse);
      expect(isFoodTruthy([]), isFalse);
      expect(isFoodTruthy({}), isFalse);
      expect(isFoodTruthy(null), isFalse);
    });

    test('mealCalorieMismatch fires only past max(100, 20%) and needs items',
        () {
      // Production case: items 135 vs total 1335.
      expect(
          mealCalorieMismatch({
            'total_calories': 1335,
            'food_items': [
              {'estimated_calories': 135}
            ],
          }),
          135);
      // Within threshold: |450-400| = 50 <= max(100, 90) → consistent.
      expect(
          mealCalorieMismatch({
            'total_calories': 450,
            'food_items': [
              {'estimated_calories': 400}
            ],
          }),
          isNull);
      // Boundary: |250-100| = 150 > max(100, 50) → flag with the int sum.
      expect(
          mealCalorieMismatch({
            'total_calories': 250,
            'food_items': [
              {'estimated_calories': 100}
            ],
          }),
          100);
      // No countable items / junk-only items / bad total / sum <= 0 → null.
      expect(mealCalorieMismatch({'total_calories': 500}), isNull);
      expect(
          mealCalorieMismatch({
            'total_calories': 500,
            'food_items': [
              {'estimated_calories': 'lots'},
              'not a map',
            ],
          }),
          isNull);
      expect(
          mealCalorieMismatch({
            'total_calories': 'junk',
            'food_items': [
              {'estimated_calories': 135}
            ],
          }),
          isNull);
      expect(
          mealCalorieMismatch({
            'total_calories': 500,
            'food_items': [
              {'estimated_calories': -10}
            ],
          }),
          isNull);
    });

    test('pyRound is banker\'s rounding like Python round()', () {
      expect(pyRound(0.5), 0);
      expect(pyRound(1.5), 2);
      expect(pyRound(2.5), 2);
      expect(pyRound(-2.5), -2);
      expect(pyRound(2.6), 3);
      expect(pyRound(189.6), 190);
      expect(pyRound(450.4), 450);
    });

    test('commaFmt mirrors Python {:,} for ints and floats', () {
      expect(commaFmt(999), '999');
      expect(commaFmt(1234567), '1,234,567');
      expect(commaFmt(-1234), '-1,234');
      expect(commaFmt(1234.5), '1,234.5');
      expect(signedCommaFmt(333), '+333');
      expect(signedCommaFmt(-667), '-667');
    });

    test('pyMedian averages the middle pair on even counts (spec §5.1)', () {
      expect(pyMedian([1600, 1800, 2000, 2200]), 1900.0);
      expect(pyMedian([3, 1, 2]), 2);
      expect(pyMedian([1, 2]), 1.5);
    });
  });

  group('todaySummary (spec §5.1/§5.2)', () {
    test('empty day', () async {
      expect(await builder.todaySummary(),
          '📋 Today\'s Summary\n\nNo meals logged yet today.');
    });

    test('hostile totals: safeNumber clamps, no negative clamp on §5.1 sums',
        () async {
      dao.meals.addAll([
        meal(
            date: '2026-07-17',
            time: '12:30 PM',
            analysis: {
              'is_food': true,
              'meal_description': 'Chicken rice',
              'total_calories': 640,
              'total_protein_g': 30,
              'total_carbs_g': 80,
              'total_fat_g': 15,
            }),
        meal(
            date: '2026-07-17',
            time: '07:15 PM',
            corrected: true,
            analysis: {
              'is_food': 1, // truthy non-bool still counts as food
              'meal_description': 'Junk',
              'total_calories': '900', // string → 0
              'total_protein_g': 1e12, // out of bounds → 0
              'total_carbs_g': -5, // kept: §5.1 totals have NO negative clamp
              'total_fat_g': true, // bool → 0
            }),
        meal(date: '2026-07-17', analysis: food(500, isFood: false)),
        meal(date: '2026-07-17', analysis: food(500, isFood: '')),
      ]);

      expect(await builder.todaySummary(), '''
📋 Today's Summary

1. Chicken rice (12:30 PM)
   ~640 kcal | P:30g C:80g F:15g
2. Junk (07:15 PM) ✏️
   ~0 kcal | P:0g C:-5g F:0g

🔥 640 kcal
🥩 Protein: 30g
🍞 Carbs: 75g
🧈 Fat: 15g
📸 Meals: 2''');
    });

    test('typical-day line: median of prior 7 days, headroom branch',
        () async {
      dao.meals.addAll([
        meal(date: '2026-07-17', analysis: food(500)),
        // Prior window [07-10, 07-16]; four days of data → even-count median.
        meal(date: '2026-07-16', analysis: food(1800)),
        meal(date: '2026-07-15', analysis: food(2200)),
        meal(date: '2026-07-14', analysis: food(1600)),
        meal(date: '2026-07-13', analysis: food(2400)),
        meal(date: '2026-07-13', analysis: food(-400)), // clamped to 0
        meal(date: '2026-07-09', analysis: food(9999)), // outside window
      ]);
      final out = await builder.todaySummary();
      // median(1800, 2200, 1600, 2400) = (1800+2200)/2 = 2000
      expect(out, contains('📊 Typical day: ~2,000 kcal'));
      expect(out, contains('⏳ ~1,500 kcal headroom vs typical'));
      expect(out, isNot(contains('above typical')));
    });

    test('typical-day line: above-typical branch and ≥2-day gate', () async {
      dao.meals.addAll([
        meal(date: '2026-07-17', analysis: food(2500)),
        meal(date: '2026-07-16', analysis: food(1900)),
      ]);
      // Only one prior day with data → no typical line.
      expect(await builder.todaySummary(), isNot(contains('Typical day')));

      dao.meals.add(meal(date: '2026-07-15', analysis: food(2100)));
      final out = await builder.todaySummary();
      expect(out, contains('📊 Typical day: ~2,000 kcal'));
      expect(out, contains('📈 ~500 kcal above typical'));
    });

    test('activity: burned > 0 renders Burned + Net with rounding', () async {
      dao.meals.add(meal(date: '2026-07-17', analysis: food(640)));
      dao.activities.add({
        'date': '2026-07-17',
        'active_calories': 450.4,
        'distance_km': 5,
        'raw': '{"steps": 8000}',
      });
      final out = await builder.todaySummary();
      expect(out, contains('🔥 Burned: 450 kcal'));
      expect(out, contains('⚖️ Net: 190 kcal')); // 640 - 450.4 → round 190
      expect(out, isNot(contains('🏃 Activity:')));
    });

    test('activity: steps/distance-only day renders an activity line',
        () async {
      dao.meals.add(meal(date: '2026-07-17', analysis: food(640)));
      dao.activities.add({
        'date': '2026-07-17',
        'distance_km': 9.2,
        'raw': {'steps': 8000},
      });
      expect(await builder.todaySummary(),
          contains('🏃 Activity: 9.2 km · 8,000 steps'));
    });

    test('activity from other days is ignored', () async {
      dao.meals.add(meal(date: '2026-07-17', analysis: food(640)));
      dao.activities.add({'date': '2026-07-16', 'active_calories': 300});
      final out = await builder.todaySummary();
      expect(out, isNot(contains('Burned')));
      expect(out, isNot(contains('🏃 Activity:')));
    });
  });

  group('history (spec §5.3)', () {
    test('empty window message uses the requested day count', () async {
      expect(await builder.history(),
          '📅 30-Day History\n\nNo meals logged in the past 30 days.');
    });

    test('per-day totals desc, Today label, negative clamp, int average',
        () async {
      dao.meals.addAll([
        meal(date: '2026-07-17', analysis: food(640)),
        meal(date: '2026-07-15', analysis: food(2000)),
        meal(date: '2026-07-15', analysis: food(-100)), // max(0, …) clamp
        meal(date: '2026-07-11', analysis: food(1500.5)),
        meal(date: '2026-07-10', analysis: food(9999)), // outside 7-day window
      ]);
      expect(await builder.history(days: 7), '''
📅 7-Day History

• Today: ~640 kcal
• Wednesday, Jul 15: ~2000 kcal
• Saturday, Jul 11: ~1500.5 kcal

📊 Average: ~1380 kcal / day''');
    });
  });

  group('dailyReport (spec §5.5)', () {
    test('no meals: header + empty-day message', () async {
      expect(await builder.dailyReport('2026-07-17'),
          '📊 Daily Calorie Report\n📅 Friday, July 17, 2026\n\nNo meals logged today. 🍽️');
    });

    test('invalid date argument throws', () {
      expect(() => builder.dailyReport('17/07/2026'), throwsArgumentError);
      expect(() => builder.dailyReport('2026-02-30'), throwsArgumentError);
    });

    test('full golden: meals, subtotal, summary, macro split', () async {
      dao.meals.add(meal(
          date: '2026-07-17',
          time: '12:30 PM',
          imageHash: 'abc',
          analysis: {
            'is_food': true,
            'meal_description': 'Grilled chicken',
            'total_calories': 450,
            'total_protein_g': 50,
            'total_carbs_g': 12,
            'total_fat_g': 22,
            'food_items': [
              {
                'name': 'Chicken',
                'estimated_calories': 280,
                'protein_g': 43,
                'carbs_g': 0,
                'fat_g': 12,
              },
              {
                'name': 'Salad',
                'estimated_calories': 170,
                'protein_g': 7,
                'carbs_g': 12,
                'fat_g': 10,
              },
            ],
          }));

      expect(await builder.dailyReport('2026-07-17'), '''
📊 Daily Calorie Report
📅 Friday, July 17, 2026

🍽️ Meals:

1. Grilled chicken — 12:30 PM
  • Chicken: 280 kcal
    P:43g | C:0g | F:12g
  • Salad: 170 kcal
    P:7g | C:12g | F:10g
  📊 Subtotal: ~450 kcal | P:50g C:12g F:22g

━━━━━━━━━━━━━━━━━━
📊 Daily Summary

🔥 Total Calories: ~450 kcal
🥩 Protein: 50g
🍞 Carbs: 12g
🧈 Fat: 22g
📸 Meals logged: 1

Macro Split:
  🥩 Protein: 45%
  🍞 Carbs: 11%
  🧈 Fat: 44%''');
    });

    test('mismatch flag fires on contradiction, suppressed when corrected',
        () async {
      Map<String, dynamic> contradiction(String desc) => {
            'is_food': true,
            'meal_description': desc,
            'total_calories': 1335,
            'food_items': [
              {'name': 'Snack', 'estimated_calories': 135}
            ],
          };
      dao.meals.addAll([
        meal(date: '2026-07-17', analysis: contradiction('Flagged')),
        meal(
            date: '2026-07-17',
            time: '01:00 PM',
            corrected: true,
            analysis: contradiction('Vouched')),
      ]);
      final out = await builder.dailyReport('2026-07-17');
      expect(
          out,
          contains('  ⚠️ Item calories sum to ~135 kcal but the meal total '
              'is ~1335 kcal — this entry may be wrong.'));
      // Exactly one flag: the corrected meal is suppressed.
      expect('⚠️ Item calories'.allMatches(out).length, 1);
      expect(out, contains('2. Vouched — 01:00 PM ✏️'));
    });

    test('duplicate flag: same hash, and (time, desc, total) fallback',
        () async {
      dao.meals.addAll([
        meal(
            date: '2026-07-17',
            time: '09:00 AM',
            imageHash: 'aaa',
            analysis: food(300)..['meal_description'] = 'Toast'),
        meal(
            date: '2026-07-17',
            time: '09:05 AM',
            imageHash: 'aaa',
            analysis: food(300)..['meal_description'] = 'Toast again'),
        meal(
            date: '2026-07-17',
            time: '08:00 AM',
            analysis: food(50)..['meal_description'] = 'Coffee'),
        meal(
            date: '2026-07-17',
            time: '08:00 AM',
            analysis: food(50)..['meal_description'] = 'Coffee'),
      ]);
      final out = await builder.dailyReport('2026-07-17');
      expect(out, contains('  ⚠️ Possible duplicate of meal 1.'));
      expect(out, contains('  ⚠️ Possible duplicate of meal 3.'));
    });

    test('7-day average: 0<cal<1e9 raw filter, ≥2 prior days, delta line',
        () async {
      dao.meals.addAll([
        meal(date: '2026-07-17', analysis: food(1500)),
        meal(date: '2026-07-16', analysis: food(2000)),
        meal(date: '2026-07-15', analysis: food(1000)),
        meal(date: '2026-07-14', analysis: food(null)..['total_calories'] = 'junk'),
        meal(date: '2026-07-13', analysis: food(-50)), // not strictly positive
        meal(date: '2026-07-12', analysis: food(3e9)), // >= 1e9
        meal(date: '2026-07-10', analysis: food(500)), // window edge, included
        meal(date: '2026-07-09', analysis: food(999)), // outside window
      ]);
      final out = await builder.dailyReport('2026-07-17');
      // Counted days: {07-16: 2000, 07-15: 1000, 07-10: 500} → avg 1167.
      expect(out, contains('📈 7-day avg: ~1,167 kcal'));
      expect(out, contains('    Today vs avg: +333 kcal (+29%)'));
    });

    test('7-day average omitted with fewer than 2 qualifying prior days',
        () async {
      dao.meals.addAll([
        meal(date: '2026-07-17', analysis: food(1500)),
        meal(date: '2026-07-16', analysis: food(2000)),
      ]);
      expect(await builder.dailyReport('2026-07-17'),
          isNot(contains('7-day avg')));
    });

    test('weight section: latest anchor + OLS trend', () async {
      dao.meals.add(meal(date: '2026-07-17', analysis: food(450)));
      dao.weights.addAll([
        {'date': '2026-07-11', 'weight_kg': 72.8},
        {'date': '2026-07-16', 'weight_kg': 72.2},
      ]);
      final out = await builder.dailyReport('2026-07-17');
      expect(out, contains('⚖️ Weight'));
      expect(out, contains('Latest: 72.2 kg'));
      // Slope: (72.2-72.8)/5 days × 7 = -0.84 kg/wk.
      expect(out, contains('7-day trend: ⬇️ -0.84 kg/wk'));
    });

    test('weight section absent without weigh-ins in the trailing window',
        () async {
      dao.meals.add(meal(date: '2026-07-17', analysis: food(450)));
      dao.weights.add({'date': '2026-07-01', 'weight_kg': 73.0});
      expect(await builder.dailyReport('2026-07-17'),
          isNot(contains('⚖️ Weight')));
    });

    test('weight section header tolerates junk weigh-in values', () async {
      dao.meals.add(meal(date: '2026-07-17', analysis: food(450)));
      dao.weights.add({'date': '2026-07-16', 'weight_kg': 'heavy'});
      final out = await builder.dailyReport('2026-07-17');
      expect(out, contains('⚖️ Weight'));
      expect(out, isNot(contains('Latest:')));
      expect(out, isNot(contains('7-day trend')));
    });
  });

  group('stats (spec §5.6)', () {
    test('empty database', () async {
      expect(await builder.stats(), '''
📈 Database Stats
Food meals today: 0
Food meals last 7 days: 0
Food meals all time: 0
Raw DB rows all time: 0
Calories last 7 days: ~0
Calories all time: ~0''');
    });

    test('truthiness, clamps, active days, source ordering', () async {
      dao.meals.addAll([
        meal(date: '2026-07-17', source: 'camera', analysis: food(640)),
        meal(
            date: '2026-07-17',
            source: 'camera',
            analysis: food(500, isFood: false)), // raw row only
        meal(
            date: '2026-07-15',
            source: '',
            analysis: food(null, isFood: 'yes')
              ..['total_calories'] = '900'), // truthy food, junk calories
        meal(
            date: '2026-07-11',
            source: 'manual_text',
            analysis: food(1000.5, isFood: 1)),
        meal(date: '2026-06-01', source: 'camera', analysis: food(2000)),
        meal(
            date: '2026-07-13',
            source: 'camera',
            analysis: food(300, isFood: [])), // falsy → raw row only
        meal(date: '2026-07-12', source: 'camera', analysis: food(5e9)),
      ]);
      expect(await builder.stats(), '''
📈 Database Stats
Food meals today: 1
Food meals last 7 days: 4
Food meals all time: 5
Raw DB rows all time: 7
Calories last 7 days: ~1,640
Calories all time: ~3,640
Average per active day: ~728 kcal

Sources
• camera: 3
• manual_text: 1
• unknown: 1''');
    });
  });

  test('createReportBuilder returns a working ReportBuilder', () async {
    final ReportBuilder b = createReportBuilder(dao);
    expect(await b.todaySummary(), contains('Summary'));
  });
}
