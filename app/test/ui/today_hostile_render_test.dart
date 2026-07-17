/// Today screen renders crafted meals including hostile analysis values
/// without crashing — coercion at render via safeNumber/safeFoodItems
/// (spec §1.2 "a poison row must not break", §3.5).
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/intl.dart';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/screens/today_screen.dart';

import 'fakes.dart';

String _iso(DateTime d) => DateFormat('yyyy-MM-dd').format(d);

Meal _meal(String date, Map<String, dynamic> analysis,
        {bool corrected = false, String time = '12:00 PM'}) =>
    Meal(
      id: 0,
      date: date,
      time: time,
      timestamp: DateTime.now().toIso8601String(),
      source: 'manual_text',
      analysis: analysis,
      corrected: corrected,
    );

void main() {
  testWidgets('hostile analysis values render without crashing',
      (tester) async {
    final dao = FakeDao();
    final today = _iso(DateTime.now());

    // Sane meal, corrected.
    dao.seed(_meal(
      today,
      {
        'is_food': true,
        'meal_description': 'Grilled chicken with salad',
        'total_calories': 450,
        'total_protein_g': 50,
        'total_carbs_g': 12,
        'total_fat_g': 22,
        'food_items': [
          {'name': 'Chicken', 'estimated_calories': 280},
        ],
      },
      corrected: true,
    ));
    // Hostile: non-list food_items, string calories, truthy int is_food.
    dao.seed(_meal(today, {
      'is_food': 1,
      'meal_description': null,
      'total_calories': 'NaN',
      'total_protein_g': true,
      'total_carbs_g': [1, 2],
      'total_fat_g': {'g': 9},
      'food_items': 'junk',
    }));
    // Hostile: huge magnitude, negative, mixed junk item entries.
    dao.seed(_meal(today, {
      'is_food': ['x'],
      'meal_description': 42,
      'total_calories': 9e99,
      'total_protein_g': -5,
      'food_items': [
        {'name': null, 'estimated_calories': 'lots'},
        'string-entry',
        42,
        {'name': 'Rice', 'estimated_calories': 1e18},
      ],
    }));
    // Falsy is_food shapes must be filtered out, not crash (spec §3.5).
    dao.seed(_meal(today, {'is_food': 0, 'total_calories': 100}));
    dao.seed(_meal(today, {'is_food': '', 'total_calories': 100}));
    dao.seed(_meal(today, <String, dynamic>{}));

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: TodayScreen(dao: dao, executor: FakeExecutor()),
      ),
    ));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    // Only the three truthy-is_food meals rendered.
    expect(find.text('Grilled chicken with salad'), findsOneWidget);
    // Header totals: 450 + safeNumber('NaN')→0 + safeNumber(9e99)→0 = 450.
    expect(find.text('450 kcal'), findsOneWidget);
    // Corrected badge on the corrected meal only.
    expect(find.byKey(const Key('correctedBadge')), findsOneWidget);
    // Card fallback: falsy total_calories renders as '?' (spec §3.5).
    expect(find.textContaining('~?'), findsNothing); // hostile ones show raw
    expect(find.textContaining('~NaN kcal'), findsOneWidget);
  });

  testWidgets('typical-day line appears with >= 2 prior days of data',
      (tester) async {
    final dao = FakeDao();
    final now = DateTime.now();
    final today = _iso(now);
    dao.seed(_meal(today, {
      'is_food': true,
      'meal_description': 'Breakfast',
      'total_calories': 400,
    }));
    // Two prior days → median available (spec §5.1); negative clamps apply.
    dao.seed(_meal(_iso(now.subtract(const Duration(days: 1))), {
      'is_food': true,
      'total_calories': 2000,
    }));
    dao.seed(_meal(_iso(now.subtract(const Duration(days: 2))), {
      'is_food': true,
      'total_calories': 1000,
    }));
    dao.seed(_meal(_iso(now.subtract(const Duration(days: 2))), {
      'is_food': true,
      'total_calories': -500, // clamped to 0, spec §5.1
    }));

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: TodayScreen(dao: dao, executor: FakeExecutor()),
      ),
    ));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    final line =
        tester.widget<Text>(find.byKey(const Key('typicalDayLine'))).data!;
    // median(2000, 1000) = 1500; headroom = 1500 − 400 = 1100.
    expect(line, contains('Typical day: ~1,500 kcal'));
    expect(line, contains('~1,100 kcal headroom'));
  });

  testWidgets('empty today shows the spec empty copy', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: TodayScreen(dao: FakeDao(), executor: FakeExecutor()),
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.text('No meals logged yet today.'), findsOneWidget);
  });
}
