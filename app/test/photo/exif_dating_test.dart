// EXIF shutter-time dating (§9 app-only, 2026-07-31). The user's ask,
// verbatim: "read the data of the photo of creation to make sure we are
// matching the food to the date exactly."
//
// Why this exists: share-sheet photos have NO library asset, and when the
// filename carries no timestamp (WeChat saves, downloads, UUID names) the
// §6.3 chain came up empty and the meal was silently dated at INTAKE time.
// The JPEG's own EXIF DateTimeOriginal is the camera's shutter stamp — the
// most exact record of when the food was actually in front of the lens.
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/photo/filename_dates.dart';
import 'package:calorie_tracker/ui/photo_pipeline.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:image/image.dart' as img;

import '../ui/fakes.dart';

/// A real 1x1 JPEG carrying real EXIF, built with the same package the
/// reader uses — no hand-crafted byte fixtures to rot.
Uint8List jpegWithExif({String? dateTimeOriginal, String? ifd0DateTime}) {
  final image = img.Image(width: 1, height: 1);
  if (dateTimeOriginal != null) {
    image.exif.exifIfd['DateTimeOriginal'] = dateTimeOriginal;
  }
  if (ifd0DateTime != null) {
    image.exif.imageIfd['DateTime'] = ifd0DateTime;
  }
  return Uint8List.fromList(img.encodeJpg(image));
}

void main() {
  group('exifCapturedAt', () {
    test('reads DateTimeOriginal (the shutter stamp)', () {
      final bytes = jpegWithExif(dateTimeOriginal: '2026:07:28 19:35:07');
      expect(exifCapturedAt(bytes), DateTime(2026, 7, 28, 19, 35, 7));
    });

    test('falls back to IFD0 DateTime when the sub-IFD tag is absent', () {
      final bytes = jpegWithExif(ifd0DateTime: '2026:07:27 08:05:00');
      expect(exifCapturedAt(bytes), DateTime(2026, 7, 27, 8, 5, 0));
    });

    test('DateTimeOriginal outranks IFD0 DateTime', () {
      final bytes = jpegWithExif(
          dateTimeOriginal: '2026:07:28 19:35:07',
          ifd0DateTime: '2026:07:29 09:00:00');
      expect(exifCapturedAt(bytes), DateTime(2026, 7, 28, 19, 35, 7));
    });

    test('no EXIF, junk bytes, and junk tag values all return null', () {
      expect(exifCapturedAt(Uint8List.fromList(List.filled(64, 7))), isNull);
      expect(exifCapturedAt(jpegWithExif()), isNull);
      expect(
          exifCapturedAt(jpegWithExif(dateTimeOriginal: 'not a date')),
          isNull);
      expect(
          exifCapturedAt(jpegWithExif(dateTimeOriginal: '2026:13:45 99:00:00')),
          isNull,
          reason: 'DateTime() would silently normalize the overflow');
    });

    test('the §6.3 validation window still applies downstream', () {
      final tooOld = jpegWithExif(dateTimeOriginal: '2020:01:01 12:00:00');
      expect(
          validateCapturedAt(exifCapturedAt(tooOld), now: DateTime.now()),
          isNull,
          reason: 'a 6-year-old EXIF date must not backdate a meal');
    });
  });

  group('pipeline dating', () {
    Future<Meal> savedMealFor(IntakePhoto photo) async {
      final dao = FakeDao();
      final pipeline = PhotoPipeline(
          dao: dao,
          analyzer: FakeAnalyzer()
            ..nextPhotoOutcome = const AnalysisOutcome(
                analysis: {'is_food': true, 'total_calories': 500},
                isFood: true,
                wall: Duration.zero));
      final outcome = await pipeline.process(photo);
      expect(outcome.kind, PhotoOutcomeKind.saved, reason: outcome.message);
      return dao.meals.single;
    }

    test('a shared photo with NO capturedAt and no filename date is dated '
        'by its EXIF — the 23:50-shared-after-midnight case', () async {
      final lastNight = DateTime.now().subtract(const Duration(days: 1));
      final stamp = '${lastNight.year}:'
          '${lastNight.month.toString().padLeft(2, '0')}:'
          '${lastNight.day.toString().padLeft(2, '0')} 23:50:00';
      final meal = await savedMealFor(IntakePhoto(
          jpegWithExif(dateTimeOriginal: stamp),
          'shared-1',
          'wx_camera_88f3.jpg', // no timestamp in the name
          deliberate: true));
      expect(meal.date,
          '${lastNight.year}-${lastNight.month.toString().padLeft(2, '0')}-'
          '${lastNight.day.toString().padLeft(2, '0')}',
          reason: 'the meal belongs to the day it was EATEN');
      expect(meal.time, '11:50 PM');
    });

    test('an intake-derived capturedAt still outranks EXIF (§6.3 order '
        'unchanged)', () async {
      final assetDate = DateTime.now().subtract(const Duration(hours: 3));
      final meal = await savedMealFor(IntakePhoto(
          jpegWithExif(dateTimeOriginal: '2026:07:01 01:01:01'),
          'asset-1',
          'IMG_1.jpg',
          capturedAt: assetDate,
          deliberate: true));
      expect(meal.time, isNot('01:01 AM'));
    });
  });
}
