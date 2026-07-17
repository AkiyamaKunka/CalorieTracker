import 'dart:typed_data';

import 'package:calorie_tracker/services/analyzer/normalize.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:image/image.dart' as img;

void main() {
  test('small image is re-encoded as JPEG without upscaling (spec §3.1)', () {
    final src = img.Image(width: 100, height: 50);
    final out = normalizeForAnalysis(
        Uint8List.fromList(img.encodePng(src)));
    expect(out, isNotNull);
    final decoded = img.decodeJpg(out!);
    expect(decoded, isNotNull);
    expect(decoded!.width, 100); // no upscale to 1568
    expect(decoded.height, 50);
  });

  test('large image is downscaled so the long side is 1568, aspect kept', () {
    final src = img.Image(width: 3000, height: 2000);
    final out = normalizeForAnalysis(
        Uint8List.fromList(img.encodeJpg(src, quality: 90)));
    expect(out, isNotNull);
    final decoded = img.decodeJpg(out!);
    expect(decoded!.width, 1568);
    // 2000 * 1568 / 3000 = 1045.33 → aspect-preserving resize
    expect(decoded.height, inInclusiveRange(1044, 1046));
  });

  test('portrait long side is the height', () {
    final src = img.Image(width: 1000, height: 4000);
    final out = normalizeForAnalysis(
        Uint8List.fromList(img.encodeJpg(src, quality: 90)));
    final decoded = img.decodeJpg(out!);
    expect(decoded!.height, 1568);
    expect(decoded.width, inInclusiveRange(391, 393));
  });

  test('EXIF orientation is baked in (spec §3.1 step 3)', () {
    final src = img.Image(width: 80, height: 40);
    // Orientation 6 = rotate 90° CW to display upright.
    src.exif.imageIfd['Orientation'] = 6;
    final out =
        normalizeForAnalysis(Uint8List.fromList(img.encodeJpg(src)));
    expect(out, isNotNull);
    final decoded = img.decodeJpg(out!);
    // Dimensions swap once the rotation is baked into the pixels.
    expect(decoded!.width, 40);
    expect(decoded.height, 80);
    // And the output carries no surviving rotation flag.
    expect(decoded.exif.imageIfd['Orientation']?.toInt() ?? 1, 1);
  });

  test('undecodable bytes return null (spec §3.1 step 6)', () {
    expect(normalizeForAnalysis(Uint8List.fromList(List.filled(64, 7))),
        isNull);
    expect(normalizeForAnalysis(Uint8List(0)), isNull);
  });

  test('output is a JPEG', () {
    final src = img.Image(width: 10, height: 10);
    final out =
        normalizeForAnalysis(Uint8List.fromList(img.encodePng(src)))!;
    // JPEG SOI marker.
    expect(out[0], 0xFF);
    expect(out[1], 0xD8);
  });
}
