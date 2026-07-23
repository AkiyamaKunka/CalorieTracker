// Watcher / backfill behavior (spec §6.1, §6.4): lookback window math,
// newest-last emission, deliberate=false, session dedup, permission flow,
// 25 MB cap (§8).
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/photo/photo_library.dart';
import 'package:calorie_tracker/services/photo/watcher.dart';
import 'package:flutter_test/flutter_test.dart';

class FakeAsset implements LibraryAsset {
  FakeAsset(this.id, this.name, this.createDateTime, this.bytes,
      {this.onRead, this.throwOnRead = false});
  @override
  final String id;
  final String name;
  @override
  final DateTime createDateTime;
  final Uint8List? bytes;
  final void Function()? onRead;
  final bool throwOnRead; // Android 11+ replies errors for unreadable assets

  @override
  Future<String> fileName() async => name;

  @override
  Future<Uint8List?> originBytes() async {
    onRead?.call();
    if (throwOnRead) throw StateError('202: get originBytes error');
    return bytes;
  }
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
              DateTime(2026, 7, 17, 8, 0, i ~/ 60, i % 60 * 16), bytesOf(1)));
      final delivered = <String>[];
      intake.attachSink((p, _) async {
        delivered.add(p.assetId);
        return true;
      });
      await expectLater(intake.backfillScan(lookbackDays: 2),
          throwsA(isA<BackfillWindowTruncated>()));
      // Everything the query returned was processed BEFORE the signal —
      // truncation warns about the unseen tail, it must not abandon work.
      expect(delivered, hasLength(backfillQueryLimit));
    });

    test('sink backpressure: next bytes are read only after the sink returns',
        () async {
      final events = <String>[];
      library.assets = [
        FakeAsset('a', 'IMG_a.jpg', DateTime(2026, 7, 17, 8), bytesOf(1),
            onRead: () => events.add('read:a')),
        FakeAsset('b', 'IMG_b.jpg', DateTime(2026, 7, 17, 9), bytesOf(2),
            onRead: () => events.add('read:b')),
      ];
      intake.attachSink((p, _) async {
        events.add('sink:${p.assetId}');
        await Future<void>.delayed(const Duration(milliseconds: 5));
        events.add('done:${p.assetId}');
        return true;
      });
      await intake.backfillScan(lookbackDays: 2);
      expect(events,
          ['read:a', 'sink:a', 'done:a', 'read:b', 'sink:b', 'done:b'],
          reason: 'reading ahead of the pipeline holds every queued '
              'photo\'s bytes in memory at once (OOM class)');
    });

    test('released photo (sink false) is re-offered by the NEXT scan; '
        'terminal is not', () async {
      library.assets = [
        FakeAsset('keep', 'IMG_k.jpg', DateTime(2026, 7, 17, 8), bytesOf(1)),
        FakeAsset('retry', 'IMG_r.jpg', DateTime(2026, 7, 17, 9), bytesOf(2)),
      ];
      final delivered = <String>[];
      intake.attachSink((p, _) async {
        delivered.add(p.assetId);
        return p.assetId != 'retry'; // 'retry' → released
      });
      await intake.backfillScan(lookbackDays: 2);
      await intake.backfillScan(lookbackDays: 2);
      expect(delivered, ['keep', 'retry', 'retry']);
    });

    test('frontier halts at the first failure and at retryable releases',
        () async {
      library.assets = [
        FakeAsset('ok1', 'IMG_1.jpg', DateTime(2026, 7, 17, 8), bytesOf(1)),
        FakeAsset('bad', 'IMG_2.jpg', DateTime(2026, 7, 17, 9), null),
        FakeAsset('ok2', 'IMG_3.jpg', DateTime(2026, 7, 17, 10), bytesOf(3)),
      ];
      intake.attachSink((p, _) async => true);
      final frontier = await intake.backfillScan(lookbackDays: 2);
      // ok2 processed fine, but the watermark may not pass 'bad'.
      expect(frontier, DateTime(2026, 7, 17, 8));
    });

    test('oversize photos are covered (permanent skip), null-bytes are not',
        () async {
      library.assets = [
        FakeAsset('big', 'IMG_big.jpg', DateTime(2026, 7, 17, 8),
            Uint8List(maxPhotoBytes + 1)),
        FakeAsset('flaky', 'IMG_f.jpg', DateTime(2026, 7, 17, 9), null),
      ];
      intake.attachSink((p, _) async => true);
      await intake.backfillScan(lookbackDays: 2);
      // Second scan: oversize NOT re-read (seen), flaky re-offered.
      var flakyReads = 0;
      library.assets = [
        FakeAsset('big', 'IMG_big.jpg', DateTime(2026, 7, 17, 8),
            Uint8List(maxPhotoBytes + 1),
            onRead: () => fail('oversize must stay covered')),
        FakeAsset('flaky', 'IMG_f.jpg', DateTime(2026, 7, 17, 9), bytesOf(2),
            onRead: () => flakyReads++),
      ];
      await intake.backfillScan(lookbackDays: 2);
      expect(flakyReads, 1);
    });

    test('safeFrontier per delivery: own createDate while clean, halted '
        'value after a failure, null when truncated', () async {
      library.assets = [
        FakeAsset('a', 'IMG_a.jpg', DateTime(2026, 7, 17, 8), bytesOf(1)),
        FakeAsset('bad', 'IMG_bad.jpg', DateTime(2026, 7, 17, 9), null),
        FakeAsset('b', 'IMG_b.jpg', DateTime(2026, 7, 17, 10), bytesOf(2)),
      ];
      final safes = <DateTime?>[];
      intake.attachSink((p, safe) async {
        safes.add(safe);
        return true;
      });
      await intake.backfillScan(lookbackDays: 2);
      expect(safes, [
        DateTime(2026, 7, 17, 8), // a: clean → its own createDate
        DateTime(2026, 7, 17, 8), // b: after bad → halted at a
      ]);

      // Truncated window: NO frontier may be reported at all.
      library.assets = List.generate(
          backfillQueryLimit,
          (i) => FakeAsset('tt$i', 'IMG_tt$i.jpg',
              DateTime(2026, 7, 17, 9, 0, i ~/ 60, i % 60 * 16), bytesOf(1)));
      safes.clear();
      await expectLater(intake.backfillScan(lookbackDays: 2),
          throwsA(isA<BackfillWindowTruncated>()));
      expect(safes, everyElement(isNull));
    });

    test('a THROWING asset (Android 11+ unreadable) skips, not aborts',
        () async {
      library.assets = [
        FakeAsset('gone', 'IMG_g.jpg', DateTime(2026, 7, 17, 8), null,
            throwOnRead: true),
        FakeAsset('fine', 'IMG_f.jpg', DateTime(2026, 7, 17, 9), bytesOf(1)),
      ];
      final delivered = <String>[];
      intake.attachSink((p, _) async {
        delivered.add(p.assetId);
        return true;
      });
      final frontier = await intake.backfillScan(lookbackDays: 2);
      expect(delivered, ['fine']); // scan survived the poison asset
      expect(frontier, isNull); // but coverage halts before it
    });

    test('stop() quiesces a batch mid-delivery', () async {
      library.assets = [
        FakeAsset('p1', 'IMG_1.jpg', DateTime(2026, 7, 17, 8), bytesOf(1)),
        FakeAsset('p2', 'IMG_2.jpg', DateTime(2026, 7, 17, 9), bytesOf(2)),
      ];
      library.permissionGranted = true;
      await intake.start();
      final delivered = <String>[];
      intake.attachSink((p, _) async {
        delivered.add(p.assetId);
        await intake.stop(); // user flips the toggle mid-batch
        return true;
      });
      await intake.backfillScan(lookbackDays: 2);
      expect(delivered, ['p1'],
          reason: 'meals must not keep auto-logging after the watcher is '
              'disabled');
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
