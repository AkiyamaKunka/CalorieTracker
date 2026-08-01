/// UI-local seams and the service bundle every screen receives.
///
/// Screens depend on core/contracts.dart plus ONLY these interfaces; the
/// adapters binding them to the concrete settings/photo modules live in
/// di.dart so an integration-time signature drift is fixed in one file.
library;

import 'dart:typed_data';

import '../core/contracts.dart';
import '../services/photo/coverage.dart';
import '../services/photo/photo_library.dart';
import 'meal_thumbs.dart';
import 'photo_pipeline.dart';

/// The settings surface the UI needs (spec §8 knobs the user edits).
/// di.dart adapts the concrete AppSettings onto this; tests use in-memory
/// fakes.
abstract class SettingsStore {
  /// The ACTIVE provider's API key — [apiKey]/[model] always read and
  /// write the provider selected by [provider].
  String get apiKey;

  /// Active AI provider: 'gemini' | 'openai' | 'anthropic' | 'server'
  /// (own server on the Claude subscription) | 'qwen' | 'doubao' | 'glm'
  /// (the mainland-China providers). String-typed so the UI seam stays
  /// free of module imports (di adapts the enum).
  String get provider;

  /// Spec §3.3 quota-pause latch: while true, analyses cannot succeed —
  /// backfill triggers skip the byte-reads entirely.
  bool get isQuotaPaused;

  /// When the §3.3 latch lifts, or null when not paused — the Settings
  /// banner shows the user WHY nothing is analyzing and until when.
  DateTime? get quotaPauseUntil;

  /// See AppSettings.canAnalyze — key present for the ACTIVE provider and no
  /// quota latch. Screens gate byte-reading sweeps on this single predicate
  /// rather than re-deriving it.
  bool get canAnalyze;
  String get model; // ACTIVE provider's model string
  int get lookbackDays; // backfill window, clamp 1–30 (spec §6.4/§8)
  String get reportTime; // 'HH:mm' local, daily-report notification time
  bool get watcherEnabled;
  String get dietaryProfile; // spec §1.3 photo-prompt appendix

  /// Base URL of the user's own server (AiProvider.server); '' when unset.
  String get serverBaseUrl;

  /// Whose subscription pays on the server: 'claude' | 'glm' | 'doubao'
  /// (AppSettings.serverBackends). Plan keys live in the server's .env.
  String get serverBackend;

  Future<void> update({
    String? apiKey,
    String? provider,
    String? model,
    int? lookbackDays,
    String? reportTime,
    bool? watcherEnabled,
    String? dietaryProfile,
    String? serverBaseUrl,
    String? serverBackend,
  });
}

/// Recent-photo picking for the Add flow grid. The photo module exposes
/// pickFromRecent; di.dart adapts it here. Returned photos MUST carry
/// deliberate=true (user-picked → reclaims failed/skipped/deleted ledger
/// rows, spec §2.3 caller policies).
abstract class RecentPhotoPicker {
  /// LIST the recent assets without reading their bytes, plus a per-asset
  /// thumbnail fetch and an on-demand original. The eager recentPhotos()
  /// this replaced pulled up to 30 ORIGINALS (25 MB each) into one list,
  /// and the grid decoded every 12 MP image full-res for a ~120 px cell —
  /// hundreds of MB resident on a screen that uses exactly one photo.
  Future<List<RecentAsset>> recentAssets({int limit = 30});
  Future<Uint8List?> thumbnail(String assetId);
  Future<IntakePhoto?> loadOriginal(RecentAsset asset);
}

/// One camera-roll entry, bytes NOT read.
class RecentAsset {
  const RecentAsset(this.id, this.fileName, this.createdAt);
  final String id;
  final String fileName;
  final DateTime createdAt;
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

  /// Meal-photo thumbnails for the list UIs; null in tests → placeholders.
  final MealThumbResolver? thumbs;

  /// The coverage audit (spec §9): null in tests / when the photo module
  /// is absent.
  final CoverageAuditor? coverage;

  /// Process one photo through the app's ONE serialized pipeline — the
  /// coverage screen's "log missing / retry failed" actions go through
  /// here so they share the global FIFO with the watcher and share sheet.
  final Future<PhotoOutcome> Function(IntakePhoto photo)? processPhoto;

  /// Library access for coverage thumbnails; null in tests.
  final PhotoLibrary? photoLibrary;

  /// Server-provider Claude OAuth re-connect; null hides the button.
  final Future<({String? url, String? error})> Function()? startClaudeAuth;
  final Future<String?> Function(String code)? completeClaudeAuth;

  /// Opens the OS app-settings page (permission remediation: once the OS
  /// stops re-showing the photo dialog, in-app re-requests are no-ops and
  /// this is the ONLY way back). Null in tests hides the buttons.
  final Future<void> Function()? openSystemSettings;

  const UiServices({
    required this.dao,
    required this.analyzer,
    required this.executor,
    required this.settings,
    required this.picker,
    required this.requestPhotoPermission,
    this.photoIntake,
    this.reports,
    this.thumbs,
    this.coverage,
    this.processPhoto,
    this.photoLibrary,
    this.startClaudeAuth,
    this.completeClaudeAuth,
    this.openSystemSettings,
  });
}
