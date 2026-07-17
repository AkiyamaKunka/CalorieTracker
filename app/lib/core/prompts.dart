/// The analysis prompts (docs/APP_PORT_SPEC.md §1) — thin wrappers over the
/// generated shared layer so the server (Python) and the app can never drift.
///
/// The prompt BODIES live in shared/prompts/*.txt and are emitted into
/// core/shared_generated.dart by scripts/sync_shared.py; this module only
/// preserves the app's original public API (foodDetectionPrompt,
/// textHandlerPrompt, withDietaryProfile) so existing callers are unchanged.
/// Do not "improve" the wording in shared/ — the server and the app must stay
/// behaviorally identical, and every rule is pinned by production behavior.
library;

import 'shared_generated.dart';

/// FOOD_DETECTION_PROMPT, sent with every photo (spec §1.1).
const String foodDetectionPrompt = sharedFoodDetectionPrompt;

/// The unified text-intent prompt. [mealsList] lines use the exact server
/// format: `[i] Date: YYYY-MM-DD | Meal: desc (~N kcal) — Items: a, b`.
/// [dietaryProfile], when non-empty, is appended the same way config.py
/// appends the profile file to the photo prompt.
String textHandlerPrompt({
  required String mealsList,
  required String userMessage,
  required String today,
  required String weekday,
  required String yesterday,
}) =>
    sharedTextHandlerPrompt(
      mealsList: mealsList,
      userMessage: userMessage,
      today: today,
      weekday: weekday,
      yesterday: yesterday,
    );

/// Optional dietary-profile appendix (mirrors config.py:83-91).
String withDietaryProfile(String prompt, String? profile) {
  final p = (profile ?? '').trim();
  if (p.isEmpty) return prompt;
  return '$prompt\n\nUser\'s Dietary Profile / Cultural Context:\n$p\n'
      'Please strongly consider these preferences when analyzing the photo.\n';
}
