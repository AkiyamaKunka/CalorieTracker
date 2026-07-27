// MealSource values are WIRE VALUES: persisted in meals.source and
// photo_ingestions.source, matched by the reclaim SQL, and emitted by
// exportJson. Renaming a constant is free; changing its STRING silently
// reinterprets existing rows — and would make the reclaim sweep stop
// matching the watcher's own writes, which is how interrupted photos got
// burned before (live incident 2026-07-23).
import 'package:calorie_tracker/core/contracts.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('wire values are frozen', () {
    expect(MealSource.appPhoto, 'app_photo');
    expect(MealSource.appWatch, 'app_watch');
    expect(MealSource.manualText, 'manual_text');
    expect(MealSource.appManual, 'app_manual');
    expect(MealSource.appManualPhoto, 'app_manual_photo');
  });

  test('all five are distinct — each records a different provenance', () {
    const all = [
      MealSource.appPhoto,
      MealSource.appWatch,
      MealSource.manualText,
      MealSource.appManual,
      MealSource.appManualPhoto,
    ];
    expect(all.toSet(), hasLength(all.length),
        reason: 'manual_text = parsed from a description; app_manual = '
            'numbers typed by hand, no model involved');
  });
}
