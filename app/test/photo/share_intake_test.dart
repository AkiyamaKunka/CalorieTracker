// Deliberate intake (spec §6, §2.3): share-sheet stream + pickFromRecent,
// all deliberate=true; §6.1 extension filter; §8 25 MB cap.
import 'dart:async';
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/photo/photo_library.dart';
import 'package:calorie_tracker/services/photo/share_intake.dart';
import 'package:flutter_test/flutter_test.dart';

import 'watcher_test.dart' show FakeAsset, FakePhotoLibrary;

class FakeShareSource implements SharedMediaSource {
  final StreamController<List<String>> controller =
      StreamController<List<String>>();
  List<String> initial = [];

  @override
  Stream<List<String>> imagePathBatches() => controller.stream;

  @override
  Future<List<String>> initialImagePaths() async => initial;
}

void main() {
  final now = DateTime(2026, 7, 17, 12, 0, 0);
  final files = <String, Uint8List>{};

  Future<Uint8List?> readBytes(String path) async => files[path];

  final cleaned = <String>[];

  ShareIntake build(FakeShareSource source, FakePhotoLibrary library) =>
      ShareIntake(
          source: source,
          library: library,
          readBytes: readBytes,
          cleanup: (path) async => cleaned.add(path),
          clock: () => now);

  setUp(() {
    files.clear();
    cleaned.clear();
  });

  group('share stream', () {
    test('shared image becomes a deliberate IntakePhoto with filename date',
        () async {
      final source = FakeShareSource();
      final intake = build(source, FakePhotoLibrary());
      files['/shared/IMG_20260716_193042.jpg'] =
          Uint8List.fromList([1, 2, 3]);

      final emitted = <IntakePhoto>[];
      final sub = intake.photos().listen(emitted.add);
      source.controller.add(['/shared/IMG_20260716_193042.jpg']);
      await pumpEventQueue();

      expect(emitted, hasLength(1));
      final photo = emitted.single;
      expect(photo.deliberate, isTrue); // §2.3 deliberate re-send policy
      expect(photo.fileName, 'IMG_20260716_193042.jpg');
      expect(photo.capturedAt, DateTime(2026, 7, 16, 19, 30, 42));
      expect(photo.bytes, [1, 2, 3]);
      // Plugin contract: the container copy is deleted once consumed —
      // without this every shared photo leaks in the app group forever.
      expect(cleaned, ['/shared/IMG_20260716_193042.jpg']);
      await sub.cancel();
    });

    test('rejected shares (bad extension / oversized) still clean up the copy',
        () async {
      final source = FakeShareSource();
      final intake = build(source, FakePhotoLibrary());
      files['/shared/notes.txt'] = Uint8List.fromList([1]);
      files['/shared/IMG_big.jpg'] =
          Uint8List.fromList(List.filled(maxPhotoBytes + 1, 0));

      final emitted = <IntakePhoto>[];
      final sub = intake.photos().listen(emitted.add);
      source.controller.add(['/shared/notes.txt', '/shared/IMG_big.jpg']);
      await pumpEventQueue();

      expect(emitted, isEmpty);
      expect(cleaned,
          containsAll(['/shared/notes.txt', '/shared/IMG_big.jpg']));
      await sub.cancel();
    });

    test('cold-start (initial) share is delivered first', () async {
      final source = FakeShareSource()
        ..initial = ['/shared/launch_meal.png'];
      files['/shared/launch_meal.png'] = Uint8List.fromList([9]);
      final intake = build(source, FakePhotoLibrary());

      final emitted = <IntakePhoto>[];
      final sub = intake.photos().listen(emitted.add);
      await pumpEventQueue();
      expect(emitted.map((p) => p.fileName), ['launch_meal.png']);
      // No filename timestamp and no asset date → capturedAt null (§6.3
      // fallback: intake-time dating happens downstream).
      expect(emitted.single.capturedAt, isNull);
      await sub.cancel();
    });

    test('unsupported extensions and unreadable/oversized files are skipped',
        () async {
      final source = FakeShareSource();
      final intake = build(source, FakePhotoLibrary());
      files['/shared/notes.txt'] = Uint8List.fromList([1]);
      files['/shared/huge.jpg'] = Uint8List(maxPhotoBytes + 1);
      // '/shared/gone.jpg' unreadable: not in the map.
      files['/shared/ok.HEIC'] = Uint8List.fromList([5]); // case-insensitive

      final emitted = <IntakePhoto>[];
      final sub = intake.photos().listen(emitted.add);
      source.controller.add([
        '/shared/notes.txt',
        '/shared/huge.jpg',
        '/shared/gone.jpg',
        '/shared/ok.HEIC',
      ]);
      await pumpEventQueue();
      expect(emitted.map((p) => p.fileName), ['ok.HEIC']);
      await sub.cancel();
    });
  });

  group('§6.1 extension filter', () {
    test('accepts jpg/jpeg/png/heic/heif/tiff, rejects the rest', () {
      for (final name in [
        'a.jpg', 'a.JPEG', 'a.png', 'a.heic', 'a.HEIF', 'a.tiff'
      ]) {
        expect(hasSupportedPhotoExtension(name), isTrue, reason: name);
      }
      for (final name in ['a.gif', 'a.mp4', 'a.txt', 'noext', 'a.']) {
        expect(hasSupportedPhotoExtension(name), isFalse, reason: name);
      }
    });
  });

  group('pickFromRecent', () {
    test('returns deliberate photos, newest first, honoring the limit',
        () async {
      final library = FakePhotoLibrary()
        ..assets = [
          FakeAsset('p1', 'IMG_20260717_080000.jpg',
              DateTime(2026, 7, 17, 8, 0), Uint8List.fromList([1])),
          FakeAsset('p2', 'IMG_20260716_080000.jpg',
              DateTime(2026, 7, 16, 8, 0), Uint8List.fromList([2])),
          FakeAsset('p3', 'IMG_20260715_080000.jpg',
              DateTime(2026, 7, 15, 8, 0), Uint8List.fromList([3])),
        ];
      final intake = build(FakeShareSource(), library);

      final picked = await intake.pickFromRecent(2);
      expect(picked.map((p) => p.assetId), ['p1', 'p2']);
      expect(picked.every((p) => p.deliberate), isTrue); // §2.3 reclaim rule
      expect(picked.first.capturedAt, DateTime(2026, 7, 17, 8, 0, 0));
    });

    test('permission denied → empty result', () async {
      final library = FakePhotoLibrary()..permissionGranted = false;
      final intake = build(FakeShareSource(), library);
      expect(await intake.pickFromRecent(5), isEmpty);
    });

    test('non-positive limit → empty without touching the library', () async {
      final library = FakePhotoLibrary();
      final intake = build(FakeShareSource(), library);
      expect(await intake.pickFromRecent(0), isEmpty);
      expect(library.permissionRequests, 0);
    });
  });
}
