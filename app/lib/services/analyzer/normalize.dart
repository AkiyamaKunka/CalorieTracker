/// Photo normalization for analysis — port of `_normalize_photo_for_analysis`
/// (spec §3.1). Required outputs: EXIF-upright, long side ≤ 1568 px (never
/// upscaled), JPEG quality 85. The Python draft()-decode step is a JPEG
/// decoder optimization, not a correctness requirement (spec §3.1 rationale).
///
/// Top-level function so callers can run it in a compute() isolate — a 12 MP
/// decode must not jank the UI thread.
library;

import 'dart:typed_data';

import 'package:image/image.dart' as img;

import '../../core/shared_generated.dart';

/// Spec §3.1/§8: long side 1568 px, matches the accuracy-neutral cap; not
/// user-editable. Value from shared/constants.json.
const int kAnalysisLongSidePx = SharedConstants.normalizeMaxDimensionPx;

/// Spec §3.1 step 5: JPEG quality 85 (shared/constants.json).
const int kAnalysisJpegQuality = SharedConstants.normalizeJpegQuality;

/// Decoded-pixel ceiling: the 25 MB BYTE cap does not bound pixels — a
/// 108 MP JPEG is ~15 MB on disk but ~432 MB as RGBA, an instant OOM in
/// the background isolate. 50 MP (~200 MB peak, one photo at a time)
/// admits every mainstream camera's full-resolution output.
const int kMaxDecodePixels = 50 * 1000 * 1000;

/// Tiny history thumbnail (spec §9 app-only): long side [kThumbLongSidePx],
/// modest quality — target ~5-15 KB so a year of meals costs a few MB.
/// Null on any failure; a meal without a thumb is display-degraded, never
/// an error. Top-level so callers can run it in a compute() isolate.
const int kThumbLongSidePx = 160;
const int kThumbJpegQuality = 70;

Uint8List? makeMealThumb(Uint8List original) {
  try {
    final decoder = img.findDecoderForData(original);
    final info = decoder?.startDecode(original);
    if (info != null && info.width * info.height > kMaxDecodePixels) {
      return null; // same decode-bomb ceiling as analysis normalization
    }
    var decoded = img.decodeImage(original);
    if (decoded == null) return null;
    decoded = img.bakeOrientation(decoded);
    final longest =
        decoded.width > decoded.height ? decoded.width : decoded.height;
    if (longest > kThumbLongSidePx) {
      decoded = decoded.width >= decoded.height
          ? img.copyResize(decoded,
              width: kThumbLongSidePx,
              interpolation: img.Interpolation.average)
          : img.copyResize(decoded,
              height: kThumbLongSidePx,
              interpolation: img.Interpolation.average);
    }
    return Uint8List.fromList(
        img.encodeJpg(decoded, quality: kThumbJpegQuality));
  } catch (_) {
    return null;
  }
}

/// Returns the normalized JPEG, or null on ANY failure — spec §3.1 step 6:
/// callers then send the original bytes if < 5 MB, else fail.
Uint8List? normalizeForAnalysis(Uint8List original) {
  try {
    // Header-probe the dimensions BEFORE committing to a full pixel decode.
    final decoder = img.findDecoderForData(original);
    final info = decoder?.startDecode(original);
    if (info != null && info.width * info.height > kMaxDecodePixels) {
      return null;
    }
    var decoded = img.decodeImage(original);
    if (decoded == null) return null;
    // Spec §3.1 step 3: bake the EXIF orientation transpose into the pixels.
    decoded = img.bakeOrientation(decoded);
    final longest =
        decoded.width > decoded.height ? decoded.width : decoded.height;
    if (longest > kAnalysisLongSidePx) {
      // Spec §3.1 step 4: thumbnail semantics — aspect preserved, downscale
      // only (the `longest >` guard above is the no-upscale rule).
      decoded = decoded.width >= decoded.height
          ? img.copyResize(decoded,
              width: kAnalysisLongSidePx,
              interpolation: img.Interpolation.linear)
          : img.copyResize(decoded,
              height: kAnalysisLongSidePx,
              interpolation: img.Interpolation.linear);
    }
    return Uint8List.fromList(
        img.encodeJpg(decoded, quality: kAnalysisJpegQuality));
  } catch (_) {
    return null; // spec §3.1 step 6: any failure → null, caller falls back
  }
}
