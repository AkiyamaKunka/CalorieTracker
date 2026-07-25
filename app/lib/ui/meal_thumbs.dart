/// Meal-thumbnail resolution for the list UIs (spec §9 app-only).
///
/// Resolution order:
///   1. the stored meal_thumbs JPEG (written by the pipeline at save time);
///   2. for meals logged BEFORE thumbs existed: the photo library by the
///      meal's fileId (asset id) — and on success the result is written
///      back to meal_thumbs, so the backfill happens lazily exactly once
///      per meal;
///   3. null → the caller renders a placeholder (text/manual meals, or the
///      gallery original is gone).
///
/// A small in-memory cache keeps scrolling smooth; negative results are
/// cached for the session too (a missing thumb would otherwise re-query the
/// library on every list rebuild).
library;

// ignore_for_file: prefer_initializing_formals

import 'dart:typed_data';

import '../core/contracts.dart';
import '../services/photo/photo_library.dart';

class MealThumbResolver {
  MealThumbResolver({required MealsDao dao, PhotoLibrary? library})
      : _dao = dao,
        _library = library;

  final MealsDao _dao;
  final PhotoLibrary? _library;

  static const int _cacheCap = 200; // ~200 × ~10 KB ≈ 2 MB ceiling
  final Map<int, Uint8List?> _cache = {};
  // In-flight dedup: a list build calls thumbFor for every visible row at
  // once; without this, each rebuild before resolve re-hits the DB/library.
  final Map<int, Future<Uint8List?>> _inFlight = {};

  Future<Uint8List?> thumbFor(Meal meal) {
    if (_cache.containsKey(meal.id)) {
      return Future.value(_cache[meal.id]);
    }
    return _inFlight.putIfAbsent(
        meal.id,
        // Braces matter: Map.remove RETURNS the removed value — the future
        // itself — and whenComplete awaits a returned future, so the arrow
        // form makes the future await itself (verified deadlock).
        () => _resolve(meal).whenComplete(() {
              _inFlight.remove(meal.id);
            }));
  }

  Future<Uint8List?> _resolve(Meal meal) async {
    var bytes = await _dao.mealThumb(meal.id);
    if (bytes == null &&
        meal.fileId.isNotEmpty &&
        meal.imageHash.isNotEmpty &&
        _library != null) {
      // Lazy backfill for pre-thumb meals; persists so it happens once.
      bytes = await _library.thumbnailByAssetId(meal.fileId);
      if (bytes != null && bytes.isNotEmpty) {
        try {
          await _dao.saveMealThumb(meal.id, bytes);
        } catch (_) {
          // Cache-only is fine; the next launch retries the persist.
        }
      }
    }
    if (_cache.length >= _cacheCap) {
      _cache.remove(_cache.keys.first); // drop oldest inserted
    }
    return _cache[meal.id] = bytes;
  }

  /// Called by the Today/day screens when returning from the editor: the
  /// meal may have been deleted (SQLite can reuse the rowid later) or its
  /// photo association changed, and a session-cached negative result would
  /// otherwise stick until restart.
  void evict(int mealId) {
    _cache.remove(mealId);
    _inFlight.remove(mealId);
  }
}
