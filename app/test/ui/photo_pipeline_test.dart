/// Photo pipeline glue (spec §2.3/§3.5/§6): reservation policies, status
/// transitions, food_items sanitization, and per-photo error containment.
library;

import 'dart:async';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/photo_pipeline.dart';

import 'fakes.dart';

IntakePhoto _photo({bool deliberate = false}) => IntakePhoto(
    Uint8List.fromList([1, 2, 3]), 'asset1', 'IMG_20260716_193042.jpg',
    deliberate: deliberate);

String get _hash =>
    md5.convert(Uint8List.fromList([1, 2, 3])).toString().toLowerCase();

Map<String, dynamic> _foodAnalysis() => {
      'is_food': true,
      'meal_description': 'Ramen',
      'total_calories': 600,
      'food_items': [
        {'name': 'Ramen', 'estimated_calories': 600},
        'junk-entry',
        42,
      ],
    };

void main() {
  test('food photo → reserved, saved meal, ledger saved, sanitized items',
      () async {
    final dao = FakeDao();
    final analyzer = FakeAnalyzer()
      ..nextPhotoOutcome = AnalysisOutcome(
          analysis: _foodAnalysis(), isFood: true, wall: Duration.zero);
    final notes = <String>[];
    final pipeline =
        PhotoPipeline(dao: dao, analyzer: analyzer, notify: notes.add);

    final out = await pipeline.process(_photo());

    expect(out.kind, PhotoOutcomeKind.saved);
    expect(dao.meals, hasLength(1));
    expect(dao.meals.single.imageHash, _hash); // md5 of ORIGINAL bytes §6.2
    expect(dao.meals.single.source, 'app_watch');
    // Hostile entries dropped before persisting (spec §3.5).
    expect(dao.meals.single.analysis['food_items'], [
      {'name': 'Ramen', 'estimated_calories': 600}
    ]);
    expect(dao.ledger[_hash], IngestionStatus.saved);
    expect(notes, hasLength(1)); // notification-style snackbar on save
  });

  test('non-food photo → skipped tombstone, no meal', () async {
    final dao = FakeDao();
    final analyzer = FakeAnalyzer()
      ..nextPhotoOutcome = const AnalysisOutcome(
          analysis: {'is_food': false}, isFood: false, wall: Duration.zero);
    final out =
        await PhotoPipeline(dao: dao, analyzer: analyzer).process(_photo());
    expect(out.kind, PhotoOutcomeKind.skipped);
    expect(out.message, 'No food detected in this photo.'); // spec §5.4 copy
    expect(dao.meals, isEmpty);
    expect(dao.ledger[_hash], IngestionStatus.skipped);
  });

  test('PERMANENT failed analysis → ledger failed, kept for retry', () async {
    final dao = FakeDao();
    final analyzer = FakeAnalyzer()
      ..nextPhotoOutcome =
          const AnalysisOutcome(error: 'bad key', wall: Duration.zero);
    final out =
        await PhotoPipeline(dao: dao, analyzer: analyzer).process(_photo());
    expect(out.kind, PhotoOutcomeKind.failed);
    expect(dao.ledger[_hash], IngestionStatus.failed);
    expect(dao.meals, isEmpty);
  });

  test('RETRYABLE failed analysis → reservation released, not burned',
      () async {
    final dao = FakeDao();
    final analyzer = FakeAnalyzer()
      ..nextPhotoOutcome = const AnalysisOutcome(
          error: 'rate limit', retryable: true, wall: Duration.zero);
    final pipeline = PhotoPipeline(dao: dao, analyzer: analyzer);
    final out = await pipeline.process(_photo());
    expect(out.kind, PhotoOutcomeKind.failed);
    // Released: NO ledger row — the next scan re-offers the photo instead
    // of dropping it forever (automated intake never reclaims 'failed').
    expect(dao.ledger.containsKey(_hash), isFalse);

    // And a later scan CAN save it once the transient trouble clears.
    analyzer.nextPhotoOutcome = AnalysisOutcome(
        analysis: _foodAnalysis(), isFood: true, wall: Duration.zero);
    final retry = await pipeline.process(_photo());
    expect(retry.kind, PhotoOutcomeKind.saved);
    expect(dao.ledger[_hash], IngestionStatus.saved);
  });

  test('bound streams process photos ONE at a time (no Gemini burst)',
      () async {
    final dao = FakeDao();
    var active = 0, maxActive = 0, calls = 0;
    final analyzer = FakeAnalyzer();
    analyzer.onAnalyze = () async {
      active++;
      calls++;
      if (active > maxActive) maxActive = active;
      await Future<void>.delayed(const Duration(milliseconds: 2));
      active--;
    };
    final pipeline = PhotoPipeline(dao: dao, analyzer: analyzer);
    final controller = StreamController<IntakePhoto>.broadcast();
    pipeline.bindStream(controller.stream);
    for (var i = 0; i < 4; i++) {
      controller.add(IntakePhoto(
          Uint8List.fromList([i, i, i]), 'a$i', 'IMG_$i.jpg'));
    }
    while (calls < 4) {
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }
    expect(maxActive, 1); // FIFO: a backfill batch must never fan out
  });

  test('watch intake never reclaims a skipped hash; deliberate add does',
      () async {
    final dao = FakeDao();
    dao.ledger[_hash] = IngestionStatus.skipped;
    final analyzer = FakeAnalyzer()
      ..nextPhotoOutcome = AnalysisOutcome(
          analysis: _foodAnalysis(), isFood: true, wall: Duration.zero);
    final pipeline = PhotoPipeline(dao: dao, analyzer: analyzer);

    // Automated intake: strict, no reclaim (spec §2.3 caller policies).
    final auto = await pipeline.process(_photo());
    expect(auto.kind, PhotoOutcomeKind.alreadyTracked);
    expect(dao.meals, isEmpty);

    // Deliberate re-add reclaims failed/skipped/deleted (spec §2.3).
    final deliberate = await pipeline.process(_photo(deliberate: true));
    expect(deliberate.kind, PhotoOutcomeKind.saved);
    expect(dao.meals.single.source, 'app_photo');
  });

  test('deliberate add inside the 5-minute duplicate window is refused',
      () async {
    final dao = FakeDao()..duplicatePhoto = true;
    final pipeline = PhotoPipeline(dao: dao, analyzer: FakeAnalyzer());
    final out = await pipeline.process(_photo(deliberate: true));
    expect(out.kind, PhotoOutcomeKind.duplicate);
    expect(dao.ledger, isEmpty); // refused before reservation (spec §2.3)
  });

  test('a throwing DAO is contained and the hash marked failed', () async {
    final dao = _ThrowingSaveDao();
    final analyzer = FakeAnalyzer()
      ..nextPhotoOutcome = AnalysisOutcome(
          analysis: _foodAnalysis(), isFood: true, wall: Duration.zero);
    final out =
        await PhotoPipeline(dao: dao, analyzer: analyzer).process(_photo());
    expect(out.kind, PhotoOutcomeKind.failed); // contained, no throw (spec §6)
    expect(dao.ledger[_hash], IngestionStatus.failed);
  });
}

class _ThrowingSaveDao extends FakeDao {
  @override
  Future<int> saveMeal(Meal meal, {IngestionStatus? markStatus}) async {
    throw StateError('disk full');
  }
}
