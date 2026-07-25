/// UI-local seams and the service bundle every screen receives.
///
/// Screens depend on core/contracts.dart plus ONLY these interfaces; the
/// adapters binding them to the concrete settings/photo modules live in
/// di.dart so an integration-time signature drift is fixed in one file.
library;

import '../core/contracts.dart';

/// The settings surface the UI needs (spec §8 knobs the user edits).
/// di.dart adapts the concrete AppSettings onto this; tests use in-memory
/// fakes.
abstract class SettingsStore {
  /// The ACTIVE provider's API key — [apiKey]/[model] always read and
  /// write the provider selected by [provider].
  String get apiKey;

  /// Active AI provider: 'gemini' | 'openai' | 'anthropic'. String-typed so
  /// the UI seam stays free of module imports (di adapts the enum).
  String get provider;

  /// Spec §3.3 quota-pause latch: while true, analyses cannot succeed —
  /// backfill triggers skip the byte-reads entirely.
  bool get isQuotaPaused;
  String get model; // ACTIVE provider's model string
  int get lookbackDays; // backfill window, clamp 1–30 (spec §6.4/§8)
  String get reportTime; // 'HH:mm' local, daily-report notification time
  bool get watcherEnabled;
  String get dietaryProfile; // spec §1.3 photo-prompt appendix

  /// Base URL of the user's own server (AiProvider.server); '' when unset.
  String get serverBaseUrl;

  Future<void> update({
    String? apiKey,
    String? provider,
    String? model,
    int? lookbackDays,
    String? reportTime,
    bool? watcherEnabled,
    String? dietaryProfile,
    String? serverBaseUrl,
  });
}

/// Recent-photo picking for the Add flow grid. The photo module exposes
/// pickFromRecent; di.dart adapts it here. Returned photos MUST carry
/// deliberate=true (user-picked → reclaims failed/skipped/deleted ledger
/// rows, spec §2.3 caller policies).
abstract class RecentPhotoPicker {
  Future<List<IntakePhoto>> recentPhotos({int limit = 30});
}

/// Everything a screen may need, built once at startup (di.dart) or from
/// fakes in widget tests.
class UiServices {
  final MealsDao dao;
  final AnalyzerService analyzer;
  final NlExecutor executor;
  final PhotoIntake? photoIntake; // null in tests that never toggle the watcher
  final ReportBuilder? reports;
  final SettingsStore settings;
  final RecentPhotoPicker picker;
  final Future<bool> Function() requestPhotoPermission;

  const UiServices({
    required this.dao,
    required this.analyzer,
    required this.executor,
    required this.settings,
    required this.picker,
    required this.requestPhotoPermission,
    this.photoIntake,
    this.reports,
  });
}
