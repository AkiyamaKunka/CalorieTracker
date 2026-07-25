// Thumb resolution (spec §9): stored thumb wins, library backfill persists
// exactly once, text meals resolve null, negative results are cached.
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/meal_thumbs.dart';
import 'package:flutter_test/flutter_test.dart';

import '../photo/watcher_test.dart' show FakePhotoLibrary;
import 'fakes.dart';

Meal photoMeal(int id, {String fileId = 'asset1', String hash = 'h1'}) => Meal(
      id: id,
      date: '2026-07-26',
      time: '12:00 PM',
      timestamp: 'x',
      source: 'app_watch',
      imageHash: hash,
      fileId: fileId,
      analysis: const {'is_food': true},
    );

Meal textMeal(int id) => Meal(
      id: id,
      date: '2026-07-26',
      time: '12:00 PM',
      timestamp: 'x',
      source: 'manual_text',
      analysis: const {'is_food': true},
    );

void main() {
  late FakeDao dao;
  late FakePhotoLibrary library;
  late MealThumbResolver resolver;

  setUp(() {
    dao = FakeDao();
    library = FakePhotoLibrary();
    resolver = MealThumbResolver(dao: dao, library: library);
  });

  test('stored thumb wins without touching the library', () async {
    dao.thumbs[7] = Uint8List.fromList([9, 9]);
    library.thumbnails['asset1'] = Uint8List.fromList([1]);
    expect(await resolver.thumbFor(photoMeal(7)), [9, 9]);
  });

  test('library backfill persists the thumb so it happens once', () async {
    library.thumbnails['asset1'] = Uint8List.fromList([5, 5, 5]);
    final first = await resolver.thumbFor(photoMeal(3));
    expect(first, [5, 5, 5]);
    expect(dao.thumbs[3], [5, 5, 5], reason: 'backfill must persist');

    // Second resolve: cache. Library gone → still served.
    library.thumbnails.clear();
    expect(await resolver.thumbFor(photoMeal(3)), [5, 5, 5]);
  });

  test('text meals resolve null and never query the library', () async {
    library.thumbnails['asset1'] = Uint8List.fromList([1]);
    expect(await resolver.thumbFor(textMeal(4)), isNull);
  });

  test('photo meal whose asset vanished resolves null (placeholder)',
      () async {
    expect(await resolver.thumbFor(photoMeal(5)), isNull);
  });

  test('evict clears one meal id', () async {
    dao.thumbs[8] = Uint8List.fromList([2]);
    await resolver.thumbFor(photoMeal(8));
    dao.thumbs[8] = Uint8List.fromList([3]);
    // Cached value until evicted.
    expect(await resolver.thumbFor(photoMeal(8)), [2]);
    resolver.evict(8);
    expect(await resolver.thumbFor(photoMeal(8)), [3]);
  });
}
