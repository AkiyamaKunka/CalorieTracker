/// Dedup identity helper (spec §6.2): a photo IS md5(ORIGINAL bytes).
///
/// Hash the untouched library-asset / shared-file bytes, never the
/// normalized JPEG — this is what keys `photo_ingestions` and
/// `meals.image_hash` and makes backfill re-scans idempotent.
library;

import 'package:crypto/crypto.dart';

import '../../core/coerce.dart' show normalizeImageHash;

/// Lowercase hex md5 of the original bytes, passed through the §2.2 hash
/// normalization so every ledger write/lookup uses the identical key.
String originalBytesMd5(List<int> originalBytes) =>
    normalizeImageHash(md5.convert(originalBytes).toString());
