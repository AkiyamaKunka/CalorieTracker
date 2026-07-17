// Dedup identity (spec §6.2): md5 of ORIGINAL bytes, normalized lowercase.
import 'dart:convert';

import 'package:calorie_tracker/core/coerce.dart';
import 'package:calorie_tracker/services/photo/photo_hash.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('known md5 vectors, lowercase hex', () {
    expect(originalBytesMd5(utf8.encode('abc')),
        '900150983cd24fb0d6963f7d28e17f72');
    expect(
        originalBytesMd5(const []), 'd41d8cd98f00b204e9800998ecf8427e');
  });

  test('already normalized: idempotent under normalizeImageHash (§2.2)', () {
    final h = originalBytesMd5(utf8.encode('meal photo bytes'));
    expect(normalizeImageHash(h), h);
    expect(h, matches(RegExp(r'^[0-9a-f]{32}$')));
  });

  test('byte-identical inputs collide, different inputs do not', () {
    final a = originalBytesMd5([1, 2, 3]);
    expect(originalBytesMd5([1, 2, 3]), a);
    expect(originalBytesMd5([1, 2, 4]), isNot(a));
  });
}
