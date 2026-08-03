/// The calorie-evidence priority ladder (user feature 2026-08-03) must be
/// INSIDE the prompt this app actually sends — the ladder lives in
/// shared/prompts/estimation_priority.txt and is spliced in at sync time,
/// so this pins the Dart half of tests/test_estimation_priority.py.
library;

import 'package:calorie_tracker/core/prompts.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('the photo prompt carries the evidence ladder, in order', () {
    const p = foodDetectionPrompt;
    final i1 = p.indexOf('Priority 1 — A NUTRITION LABEL');
    final i2 = p.indexOf('Priority 2 — A PRINTED WEIGHT');
    final i3 = p.indexOf('Priority 3 — A BRAND, CHAIN LOGO, OR MERCHANT');
    final i4 = p.indexOf('Priority 4 — VISUAL ESTIMATION');
    expect([i1, i2, i3, i4], everyElement(greaterThanOrEqualTo(0)));
    expect(i1, lessThan(i2));
    expect(i2, lessThan(i3));
    expect(i3, lessThan(i4));
    expect(p, isNot(contains('<<ESTIMATION_PRIORITY>>')));
    // China-specific label rules: kJ-per-100g conversion + net weight.
    expect(p, contains('kJ ÷ 4.184'));
    expect(p, contains('净含量'));
  });
}
