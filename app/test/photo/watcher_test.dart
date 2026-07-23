// Watcher / backfill behavior (spec §6.1, §6.4): lookback window math,
// newest-last emission, deliberate=false, session dedup, permission flow,
// 25 MB cap (§8).
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/photo/photo_library.dart';
import 'package:calorie_tracker/services/photo/watcher.dart';
import 'package:flutter_test/flutter_test.dart';

class FakeAsset implements LibraryAsset {
  FakeAsset(this.id, this.name, this.createDateTime, this.bytes);
  @override
  final String id;
  final String name;
  @override
  final DateTime createDateTime;
  final Uint8List? bytes;

  @override
  Future<String> fileName() async => name;

  @override
  Future<Uint8List?> originBytes() async => bytes;
}

class FakePhotoLibrary implements PhotoLibrary {
  List<FakeAsset> assets = [];
  bool permissionGranted = true;
  int permissionRequests = 0;
  void Function()? changeListener;
  final List<DateTime> queriedCutoffs = [];

  @override
  Future<bool> requestPermission() async {
    permissionRequests++;
    return permissionGranted;
  }

  @override
  Future<List<LibraryAsset>> imagesCreatedAfter(DateTime cutoff,
      {int limit = 500}) async {
    queriedCutoffs.add(cutoff);
    return assets
        .where((a) => !a.createDateTime.isBefore(cutoff))
        .take(limit)
        .toList();
  }

  @override
  Future<List<LibraryAsset>> recentImages(int limit) async {
    final sorted = [...assets]
      ..sort((a, b) => b.createDateTime.compareTo(a.createDateTime));
    return sorted.take(limit).toList();
  }

  @override
  Future<void> startChangeNotify(void Function() onChange) async {
    changeListener = onChange;
  }

  @override
  Future<void> stopChangeNotify() async {
    changeListener = null;
  }
}

Uint8List bytesOf(int n, [int len = 3]) =>
    Uint8List.fromList(List.filled(len, n));

void main() {
  final now = DateTime(2026, 7, 17, 12, 0, 0);

  group('lookbackCutoff (§6.4 window math)', () {
    test('2 days = today + yesterday, cut at local midnight', () {
      expect(lookbackCutoff(now, 2), DateTime(2026, 7, 16));
    });
    test('1 day = today only', () {
      expect(lookbackCutoff(now, 1), DateTime(2026, 7, 17));
    });
    test('clamps to the §8 range 1–30', () {
      expect(lookbackCutoff(now, 0), DateTime(2026, 7, 17)); // → 1
      expect(lookbackCutoff(now, -5), DateTime(2026, 7, 17)); // → 1
      expect(lookbackCutoff(now, 99), DateTime(2026, 6, 18)); // → 30
    });
  });

  group('backfillScan', () {
    late FakePhotoLibrary library;
    late PhotoIntake intake;
    late List<IntakePhoto> emitted;

    setUp(() {
      library = FakePhotoLibrary();
      intake = LibraryPhotoIntake(
          lookbackDays: () => 2, library: library, clock: () => now);
      emitted = [];
      intake.photos.listen(emitted.add);
    });

    test('since AFTER the window cutoff narrows the scan (watermark)',
        () async {
      final mark = DateTime(2026, 7, 17, 9, 30);
      await intake.backfillScan(lookbackDays: 2, since: mark);
      expect(library.queriedCutoffs.single, mark);
    });

    test('since BEFORE the window cutoff is ignored (never widens §8 clamp)',
        () async {
      await intake.backfillScan(
          lookbackDays: 2, since: DateTime(2026, 7, 1));
      expect(library.queriedCutoffs.single, DateTime(2026, 7, 16));
    });

    test('a capped result set emits everything, then signals truncation',
        () async {
      library.assets = List.generate(
          backfillQueryLimit,
          (i) => FakeAsset('t$i', 'IMG_t$i.jpg',
              DateTime(2026, 7, 17, 8, 0, i ~/ 60, i % 60 * 16), null));
      await expectLater(
          intake.backfillScan(lookbackDays: 2),
          throwsA(isA<BackfillWindowTruncated>()));
    });

    test('window honors lookbackDays; older photos excluded', () async {
      library.assets = [
        FakeAsset('a', 'IMG_20260717_080000.jpg',
            DateTime(2026, 7, 17, 8, 0), bytesOf(1)),
        FakeAsset('b', 'IMG_20260716_080000.jpg',
            DateTime(2026, 7, 16, 8, 0), bytesOf(2)),
        FakeAsset('c', 'IMG_20260715_235900.jpg',
            DateTime(2026, 7, 15, 23, 59), bytesOf(3)), // outside 2-day window
      ];
      await intake.backfillScan(lookbackDays: 2);
      await pumpEventQueue();
      expect(emitted.map((p) => p.assetId), ['b', 'a']);
    });

    test('emission is newest-LAST and deliberate=false with metadata', () async {
      library.assets = [
        FakeAsset('new', 'IMG_20260717_090000.jpg',
            DateTime(2026, 7, 17, 9, 0), bytesOf(9)),
        FakeAsset('old', 'IMG_20260716_070000.jpg',
            DateTime(2026, 7, 16, 7, 0), bytesOf(7)),
      ];
      await intake.backfillScan();
      await pumpEventQueue();
      expect(emitted.map((p) => p.assetId), ['old', 'new']);
      final old = emitted.first;
      expect(old.deliberate, isFalse); // §2.3: automated intake is strict
      expect(old.fileName, 'IMG_20260716_070000.jpg');
      expect(old.capturedAt, DateTime(2026, 7, 16, 7, 0, 0));
      expect(old.bytes, bytesOf(7));
    });

    test('lookbackDays unset uses the settings-provided default (2)', () async {
      await intake.backfillScan();
      expect(library.queriedCutoffs.single, DateTime(2026, 7, 16));
    });

    test('null/empty bytes skipped; >25 MB refused before decode (§8)',
        () async {
      library.assets = [
        FakeAsset('nul', 'IMG_20260717_080000.jpg',
            DateTime(2026, 7, 17, 8, 0), null),
        FakeAsset('big', 'IMG_20260717_081000.jpg',
            DateTime(2026, 7, 17, 8, 10),
            Uint8List(maxPhotoBytes + 1)),
        FakeAsset('ok', 'IMG_20260717_082000.jpg',
            DateTime(2026, 7, 17, 8, 20), bytesOf(1)),
      ];
      await intake.backfillScan();
      await pumpEventQueue();
      expect(emitted.map((p) => p.assetId), ['ok']);
    });

    test('capturedAt falls back to asset create date on junk names', () async {
      library.assets = [
        FakeAsset('x', 'restored.jpeg', DateTime(2026, 7, 16, 18, 5),
            bytesOf(4)),
      ];
      await intake.backfillScan();
      await pumpEventQueue();
      expect(emitted.single.capturedAt, DateTime(2026, 7, 16, 18, 5));
    });
  });

  group('change-notify watch (§6.1)', () {
    test('start requests permission and subscribes; new assets emit once',
        () async {
      final library = FakePhotoLibrary();
      final intake = LibraryPhotoIntake(
          lookbackDays: () => 2, library: library, clock: () => now);
      final emitted = <IntakePhoto>[];
      intake.photos.listen(emitted.add);

      await intake.start();
      expect(library.permissionRequests, 1);
      expect(library.changeListener, isNotNull);

      library.assets = [
        FakeAsset('n1', 'IMG_20260717_120100.jpg',
            now.add(const Duration(minutes: 1)), bytesOf(1)),
      ];
      library.changeListener!();
      await pumpEventQueue();
      expect(emitted.map((p) => p.assetId), ['n1']);

      // A second change notification must not re-emit the same asset.
      library.changeListener!();
      await pumpEventQueue();
      expect(emitted.length, 1);
    });

    test('permission denied: silent no-op, no subscription', () async {
      final library = FakePhotoLibrary()..permissionGranted = false;
      final intake = LibraryPhotoIntake(
          lookbackDays: () => 2, library: library, clock: () => now);
      await intake.start();
      expect(library.changeListener, isNull);
    });

    test('stop unsubscribes', () async {
      final library = FakePhotoLibrary();
      final intake = LibraryPhotoIntake(
          lookbackDays: () => 2, library: library, clock: () => now);
      await intake.start();
      expect(library.changeListener, isNotNull);
      await intake.stop();
      expect(library.changeListener, isNull);
    });
  });
}
