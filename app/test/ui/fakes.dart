/// In-memory fakes of the core contracts + UI seams for widget tests.
/// No network, no platform channels.
library;

import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/foundation.dart' show ChangeNotifier;

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/photo_pipeline.dart';
import 'package:calorie_tracker/ui/services.dart';

import '../support/fake_meals_dao.dart';

export '../support/fake_meals_dao.dart' show BaseFakeDao;

/// The ui suite's DAO: everything from [BaseFakeDao] plus the cascade-style
/// `seed(Meal)` these tests use. (The base deliberately declares no seed —
/// the nl suite's differs in signature.)
class FakeDao extends BaseFakeDao {
  void seed(Meal meal) => put(meal);
}

class FakeAnalyzer implements AnalyzerService {
  AnalysisOutcome nextPhotoOutcome =
      const AnalysisOutcome(wall: Duration.zero);
  Map<String, dynamic>? nextTextIntent;

  /// Test hook: awaited inside analyzePhoto (concurrency probes).
  Future<void> Function()? onAnalyze;

  /// Test hook: controls validateKey resolution. Default resolves null (OK).
  Future<String?> Function(String apiKey)? onValidateKey;

  @override
  Future<AnalysisOutcome> analyzePhoto(Uint8List originalBytes) async {
    if (onAnalyze != null) await onAnalyze!();
    return nextPhotoOutcome;
  }

  @override
  Future<Map<String, dynamic>?> textIntent(String prompt) async =>
      nextTextIntent;

  @override
  Future<String?> validateKey(String apiKey) =>
      onValidateKey?.call(apiKey) ?? Future.value(null);

  /// Diagnostics-facing probe. Default derives from [onValidateKey] like
  /// the contract's own default; [onProbeKey] overrides it for the cases
  /// validateKey CANNOT express (out of credit vs rate-limited).
  Future<KeyProbe> Function(String apiKey)? onProbeKey;

  @override
  Future<KeyProbe> probeKey(String apiKey) async {
    if (onProbeKey != null) return onProbeKey!(apiKey);
    final error = await validateKey(apiKey);
    return error == null
        ? const KeyProbe(KeyProbeResult.ok)
        : KeyProbe(KeyProbeResult.rejected, message: error);
  }
}

class FakeExecutor implements NlExecutor {
  List<NlReply> nextReplies = const [NlReply('ok')];
  final List<String> handledTexts = [];
  final List<List<int>> confirmedDeletes = [];
  String confirmResult = 'Deleted.';

  @override
  Future<List<NlReply>> handleText(String userText) async {
    handledTexts.add(userText);
    return nextReplies;
  }

  @override
  Future<String> confirmPendingDelete(List<int> mealIds) async {
    confirmedDeletes.add(mealIds);
    return confirmResult;
  }

  /// Scripted describe result; default is a simple food analysis.
  DescribeOutcome nextDescribe = const DescribeOutcome(analysis: {
    'is_food': true,
    'meal_description': 'Beef noodle soup',
    'total_calories': 650,
    'total_protein_g': 30,
    'total_carbs_g': 80,
    'total_fat_g': 18,
  });
  final List<String> describedTexts = [];

  @override
  Future<DescribeOutcome> describeMeal(String text) async {
    describedTexts.add(text);
    return nextDescribe;
  }
}

class FakeSettings with ChangeNotifier implements SettingsStore {
  /// Provider-scoped keys, like the real store: [apiKey] reads/writes the
  /// CURRENT provider's slot so tests can catch cross-provider misrouting.
  final Map<String, String> keysByProvider;

  @override
  String get apiKey => keysByProvider[provider] ?? '';
  set apiKey(String v) => keysByProvider[provider] = v;
  @override
  String provider = 'gemini';
  @override
  bool isQuotaPaused = false;
  @override
  DateTime? quotaPauseUntil;
  @override
  bool get canAnalyze => apiKey.trim().isNotEmpty && !isQuotaPaused;
  /// Provider-scoped MODELS too — the real store keeps one per provider,
  /// and a shared field made provider-switch tests pass while production
  /// would have shown Gemini's model under Doubao (review 2026-07-31).
  final Map<String, String> modelsByProvider;

  @override
  String get model => modelsByProvider[provider] ?? '';
  set model(String v) => modelsByProvider[provider] = v;
  @override
  int lookbackDays;
  @override
  String reportTime;
  @override
  bool watcherEnabled;
  @override
  String dietaryProfile;
  @override
  String serverBaseUrl = '';
  @override
  String serverBackend = 'claude';
  int updateCalls = 0;

  FakeSettings({
    String apiKey = 'k',
    String model = 'gemini-2.5-flash',
    Map<String, String>? keys,
    Map<String, String>? models,
    this.lookbackDays = 2,
    this.reportTime = '21:00',
    this.watcherEnabled = false,
    this.dietaryProfile = '',
  })  : keysByProvider = keys ?? {'gemini': apiKey},
        modelsByProvider = models ??
            {
              'gemini': model,
              'openai': 'gpt-4o-mini',
              'anthropic': 'claude-sonnet-5',
              'qwen': 'qwen3-vl-flash',
              'doubao': 'doubao-seed-2-0-mini-260428',
              'glm': 'glm-4.6v-flash',
              'server': 'server',
            };

  @override
  String appLanguage = 'system';

  @override
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
    String? appLanguage,
  }) async {
    updateCalls++;
    if (appLanguage != null) this.appLanguage = appLanguage;
    if (apiKey != null) this.apiKey = apiKey;
    if (provider != null) this.provider = provider;
    if (model != null) this.model = model;
    if (lookbackDays != null) this.lookbackDays = lookbackDays;
    if (reportTime != null) this.reportTime = reportTime;
    if (watcherEnabled != null) this.watcherEnabled = watcherEnabled;
    if (dietaryProfile != null) this.dietaryProfile = dietaryProfile;
    if (serverBaseUrl != null) this.serverBaseUrl = serverBaseUrl;
    if (serverBackend != null) this.serverBackend = serverBackend;
    // The real AppSettings notifies on every write; the app's live-locale
    // rebuild rides on it, so the fake must too.
    notifyListeners();
  }
}

class FakePicker implements RecentPhotoPicker {
  List<IntakePhoto> photos = const [];

  /// Assets derive from [photos] so suites keep seeding one list.
  /// [loadedOriginals] proves the grid reads ORIGINAL bytes for the tapped
  /// photo ONLY.
  final List<String> loadedOriginals = [];
  final List<String> thumbnailed = [];

  @override
  Future<List<RecentAsset>> recentAssets({int limit = 30}) async => [
        for (final p in photos.take(limit))
          RecentAsset(p.assetId, p.fileName,
              p.capturedAt ?? DateTime(2026, 7, 31)),
      ];

  @override
  Future<Uint8List?> thumbnail(String assetId) async {
    thumbnailed.add(assetId);
    final match = photos.where((p) => p.assetId == assetId);
    return match.isEmpty ? null : match.first.bytes;
  }

  @override
  Future<IntakePhoto?> loadOriginal(RecentAsset asset) async {
    loadedOriginals.add(asset.id);
    final match = photos.where((p) => p.assetId == asset.id);
    if (match.isEmpty) return null;
    final p = match.first;
    return IntakePhoto(p.bytes, p.assetId, p.fileName,
        capturedAt: p.capturedAt, deliberate: true);
  }
}

class FakeIntake implements PhotoIntake {
  final StreamController<IntakePhoto> controller =
      StreamController<IntakePhoto>.broadcast();
  bool started = false;
  Future<bool> Function(IntakePhoto, DateTime?)? sink;

  @override
  Stream<IntakePhoto> get photos => controller.stream;

  @override
  void attachSink(
          Future<bool> Function(IntakePhoto photo, DateTime? safeFrontier)?
              s) =>
      sink = s;

  int backfillScans = 0;

  @override
  Future<DateTime?> backfillScan(
      {int lookbackDays = 2, DateTime? since}) async {
    backfillScans++;
    return null;
  }

  @override
  Future<void> start() async => started = true;

  @override
  Future<void> stop() async => started = false;
}

class FakeReports implements ReportBuilder {
  @override
  Future<String> todaySummary() async => '';
  @override
  Future<String> history({int days = 30}) async => '';
  @override
  Future<String> dailyReport(String date) async => '';
  @override
  Future<String> stats() async => '';
}

UiServices makeServices({
  FakeDao? dao,
  FakeAnalyzer? analyzer,
  FakeExecutor? executor,
  FakeSettings? settings,
  FakePicker? picker,
  FakeIntake? intake,
  bool grantPhotoPermission = true,
  Future<PhotoOutcome> Function(IntakePhoto photo)? processPhoto,
}) {
  final s = settings ?? FakeSettings();
  return UiServices(
    dao: dao ?? FakeDao(),
    analyzer: analyzer ?? FakeAnalyzer(),
    executor: executor ?? FakeExecutor(),
    settings: s,
    // Same wiring as production di.dart: the settings object IS the
    // change signal the app re-locales on.
    settingsChanges: s,
    picker: picker ?? FakePicker(),
    photoIntake: intake ?? FakeIntake(),
    reports: FakeReports(),
    requestPhotoPermission: () async => grantPhotoPermission,
    processPhoto: processPhoto,
  );
}
