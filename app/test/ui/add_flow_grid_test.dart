// The recent-photos grid's MEMORY contract (rewritten 2026-07-31).
//
// It used to read up to THIRTY originals (25 MB each) before showing
// anything, hold them all alive for the screen's lifetime, and decode
// every 12 MP image full-res into a ~120 px cell. On a mid-range phone
// that is hundreds of MB for a screen that ends up using exactly ONE
// photo. These tests pin the new shape: list assets, thumbnail per cell,
// original bytes only for the tapped photo.
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/photo_pipeline.dart';
import 'package:calorie_tracker/ui/screens/add_flow.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'fakes.dart';

IntakePhoto _photo(String id) => IntakePhoto(
    Uint8List.fromList(List.filled(16, 3)), id, '$id.jpg',
    capturedAt: DateTime(2026, 7, 30, 12));

void main() {
  testWidgets('the grid reads THUMBNAILS only — no originals until a tap',
      (tester) async {
    final picker = FakePicker()
      ..photos = [for (var i = 0; i < 12; i++) _photo('a$i')];
    final services = makeServices(picker: picker);

    await tester.pumpWidget(
        MaterialApp(home: AddPhotoScreen(services: services)));
    await tester.pumpAndSettle();

    expect(picker.loadedOriginals, isEmpty,
        reason: 'showing the grid must not read a single original');
    expect(picker.thumbnailed, isNotEmpty,
        reason: 'cells render from thumbnails');
    expect(find.byKey(const Key('recentPhoto0')), findsOneWidget);
  });

  testWidgets('tapping reads exactly ONE original and runs it through the '
      'app pipeline as a DELIBERATE add', (tester) async {
    final picker = FakePicker()..photos = [_photo('a0'), _photo('a1')];
    final processed = <IntakePhoto>[];
    final services = makeServices(
        picker: picker,
        processPhoto: (photo) async {
          processed.add(photo);
          return const PhotoOutcome(
              PhotoOutcomeKind.saved, 'Meal logged: ok');
        });

    await tester.pumpWidget(
        MaterialApp(home: AddPhotoScreen(services: services)));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('recentPhoto1')));
    await tester.pumpAndSettle();

    expect(picker.loadedOriginals, ['a1'],
        reason: 'ONLY the tapped photo costs an original read');
    expect(processed.single.assetId, 'a1');
    expect(processed.single.deliberate, isTrue,
        reason: 'user-picked adds reclaim skipped/failed ledger rows '
            '(spec §2.3)');
    expect(processed.single.capturedAt, isNotNull,
        reason: '§6.3 dating must survive the lazy path');
  });

  testWidgets('an unreadable original says so instead of hanging on the '
      'spinner', (tester) async {
    // loadOriginal returns null for a photo that vanished or exceeds the
    // 25 MB cap — the old eager path silently dropped those from the grid.
    final picker = FakePicker()..photos = [_photo('gone')];
    final services = makeServices(picker: picker);
    await tester.pumpWidget(
        MaterialApp(home: AddPhotoScreen(services: services)));
    await tester.pumpAndSettle();
    picker.photos = const []; // the asset disappears before the tap
    await tester.tap(find.byKey(const Key('recentPhoto0')));
    await tester.pumpAndSettle();
    expect(find.textContaining('could not be read'), findsOneWidget);
    expect(find.byKey(const Key('photoAnalyzing')), findsNothing);
  });
}
