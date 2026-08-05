// AUTOMATIC leftover detection (2026-08-05): the watcher's own analysis
// call carries today's meals; a confident leftover_of reply ADJUSTS the
// original instead of logging a double-count. These pin the gate, the
// application, and every fall-through.
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/core/leftover_logic.dart';
import 'package:calorie_tracker/ui/format.dart' show isoDate;
import 'package:calorie_tracker/ui/photo_pipeline.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

Meal _meal(int id, String time, num kcal) => Meal(
      id: id,
      date: isoDate(DateTime.now()),
      time: time,
      timestamp: '${isoDate(DateTime.now())}T08:00:0$id',
      source: 'app_watch',
      imageHash: 'orig-$id',
      analysis: {
        'is_food': true,
        'meal_description': 'Cafeteria tray $id',
        'total_calories': kcal,
        'total_protein_g': 40,
        'total_carbs_g': 90,
        'total_fat_g': 40,
        'food_items': [
          {'name': 'Rice (~200 g)', 'estimated_calories': 300},
          {'name': 'Pork (~150 g)', 'estimated_calories': kcal - 300},
        ],
      },
    );

Map<String, dynamic> _leftoverReply(
        {num index = 0, double confidence = 0.9}) =>
    {
      'is_food': true,
      'leftover_of': index,
      'confidence': confidence,
      'leftover_fraction': 0.4,
      'items': [
        {'name': 'Rice (~200 g)', 'left_fraction': 1.0},
        {'name': 'Pork (~150 g)', 'left_fraction': 0.0},
      ],
      'note': 'same tray, rice untouched',
    };

void main() {
  late FakeDao dao;
  late FakeAnalyzer analyzer;
  late PhotoPipeline pipeline;
  final notifications = <String>[];

  setUp(() {
    dao = FakeDao();
    analyzer = FakeAnalyzer();
    notifications.clear();
    pipeline = PhotoPipeline(
        dao: dao,
        analyzer: analyzer,
        hasher: (_) async => 'leftover-hash-1',
        notify: notifications.add);
  });

  IntakePhoto photo() =>
      IntakePhoto(Uint8List.fromList([7]), 'w1', 'IMG_7.jpg');

  test('a confident leftover_of ADJUSTS the original — no new meal',
      () async {
    dao.put(_meal(1, '01:34 PM', 965));
    analyzer.nextPhotoOutcome = AnalysisOutcome(
        analysis: _leftoverReply(), isFood: true, wall: Duration.zero);
    final out = await pipeline.process(photo());
    expect(out.kind, PhotoOutcomeKind.leftoverApplied);
    expect(dao.meals, hasLength(1), reason: 'no double-count row');
    final updated = dao.meals.single;
    expect(updated.analysis['total_calories'], 665); // pork eaten, rice left
    expect(updated.analysis['leftover']['original']['total_calories'], 965);
    expect(dao.ledger['leftover-hash-1'], IngestionStatus.skipped);
    expect(notifications.single, contains('Leftovers deducted'));
    // The prompt data really carried the candidate.
    expect(analyzer.lastRecentMeals, hasLength(1));
    expect(analyzer.lastRecentMeals!.single['meal_description'],
        'Cafeteria tray 1');
  });

  test('low confidence falls through to a NORMAL new meal', () async {
    dao.put(_meal(1, '01:34 PM', 965));
    analyzer.nextPhotoOutcome = AnalysisOutcome(
        analysis: _leftoverReply(confidence: 0.4),
        isFood: true,
        wall: Duration.zero);
    final out = await pipeline.process(photo());
    expect(out.kind, PhotoOutcomeKind.saved);
    expect(dao.meals, hasLength(2),
        reason: 'uncertain verdicts must not silently shrink a real meal');
    expect(dao.meals.first.analysis['total_calories'], 965,
        reason: 'the original is untouched');
  });

  test('an out-of-range index falls through to a normal meal', () async {
    dao.put(_meal(1, '01:34 PM', 965));
    analyzer.nextPhotoOutcome = AnalysisOutcome(
        analysis: _leftoverReply(index: 7),
        isFood: true,
        wall: Duration.zero);
    final out = await pipeline.process(photo());
    expect(out.kind, PhotoOutcomeKind.saved);
    expect(dao.meals, hasLength(2));
  });

  test('an empty day sends no candidates and a leftover_of reply cannot '
      'match anything', () async {
    analyzer.nextPhotoOutcome = AnalysisOutcome(
        analysis: _leftoverReply(), isFood: true, wall: Duration.zero);
    final out = await pipeline.process(photo());
    expect(analyzer.lastRecentMeals, isEmpty);
    expect(out.kind, PhotoOutcomeKind.saved);
    expect(dao.meals, hasLength(1));
  });

  test('only the LAST five meals ride along, newest last', () async {
    for (var i = 1; i <= 7; i++) {
      dao.put(_meal(i, '0$i:00 AM', 400));
    }
    analyzer.nextPhotoOutcome =
        const AnalysisOutcome(analysis: {'is_food': false}, wall: Duration.zero);
    await pipeline.process(photo());
    expect(analyzer.lastRecentMeals, hasLength(5));
    expect(analyzer.lastRecentMeals!.last['meal_description'],
        'Cafeteria tray 7');
    expect(analyzer.lastRecentMeals!.first['meal_description'],
        'Cafeteria tray 3');
  });

  test('the Dart block format matches the server byte-for-byte', () {
    final block = formatRecentMealsBlock([
      recentMealCompact(time: '08:05 AM', analysis: {
        'meal_description': 'Soy milk',
        'total_calories': 110.4,
        'food_items': [
          {'name': 'Soy milk (~250 mL)', 'estimated_calories': 110},
        ],
      }),
    ]);
    expect(
        block,
        '\nRECENT MEALS (today, for the leftover check):\n'
        '[0] 08:05 AM — Soy milk (~110 kcal): '
        'Soy milk (~250 mL) (~110 kcal)');
  });
}
