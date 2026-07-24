/// In-memory fakes of the core contracts + UI seams for widget tests.
/// No network, no platform channels.
library;

import 'dart:async';
import 'dart:typed_data';

import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/ui/services.dart';

class FakeDao implements MealsDao {
  final List<Meal> meals = [];
  final Map<String, IngestionStatus> ledger = {};
  int _nextId = 1;
  bool duplicatePhoto = false;

  void seed(Meal meal) {
    meals.add(Meal(
      id: meal.id == 0 ? _nextId++ : meal.id,
      date: meal.date,
      time: meal.time,
      timestamp: meal.timestamp,
      source: meal.source,
      imageHash: meal.imageHash,
      fileId: meal.fileId,
      analysis: meal.analysis,
      corrected: meal.corrected,
    ));
  }

  @override
  Future<int> saveMeal(Meal meal, {IngestionStatus? markStatus}) async {
    final id = _nextId++;
    meals.add(Meal(
      id: id,
      date: meal.date,
      time: meal.time,
      timestamp: meal.timestamp,
      source: meal.source,
      imageHash: meal.imageHash,
      fileId: meal.fileId,
      analysis: meal.analysis,
      corrected: meal.corrected,
    ));
    if (markStatus != null && meal.imageHash.isNotEmpty) {
      ledger[meal.imageHash] = markStatus;
    }
    return id;
  }

  @override
  Future<void> updateMealAnalysis(
      int mealId, Map<String, dynamic> analysis) async {}

  @override
  Future<void> deleteMeal(int mealId) async {
    meals.removeWhere((m) => m.id == mealId);
  }

  @override
  Future<List<Meal>> mealsBetween(String startDate, String endDate) async =>
      meals
          .where((m) =>
              m.date.compareTo(startDate) >= 0 && m.date.compareTo(endDate) <= 0)
          .toList();

  @override
  Future<List<Meal>> recentMeals({int days = 7}) async => List.of(meals);

  @override
  Future<bool> isDuplicatePhoto(String imageHash) async => duplicatePhoto;

  @override
  Future<bool> reservePhotoHash(String imageHash,
      {required String source, bool reclaimDeliberate = false}) async {
    final existing = ledger[imageHash];
    if (existing == null) {
      ledger[imageHash] = IngestionStatus.processing;
      return true;
    }
    if (reclaimDeliberate &&
        const {
          IngestionStatus.failed,
          IngestionStatus.skipped,
          IngestionStatus.deleted
        }.contains(existing)) {
      ledger[imageHash] = IngestionStatus.processing;
      return true;
    }
    return false;
  }

  @override
  Future<void> markPhotoHash(String imageHash, IngestionStatus status,
      {int? mealId}) async {
    ledger[imageHash] = status;
  }

  @override
  Future<void> releasePhotoHash(String imageHash) async {
    if (ledger[imageHash] == IngestionStatus.processing) {
      ledger.remove(imageHash);
    }
  }

  @override
  Future<void> reclaimStaleProcessing() async {}

  @override
  Future<void> saveBodyWeight(String date, double kg) async {}

  @override
  Future<void> saveActivity(String date,
      {num? activeCalories, int? steps, double? distanceKm}) async {}

  @override
  Future<String> exportJson() async => '{}';
}

class FakeAnalyzer implements AnalyzerService {
  AnalysisOutcome nextPhotoOutcome =
      const AnalysisOutcome(wall: Duration.zero);
  Map<String, dynamic>? nextTextIntent;

  /// Test hook: awaited inside analyzePhoto (concurrency probes).
  Future<void> Function()? onAnalyze;

  /// Test hook: controls validateKey resolution. Default resolves null (OK).
  Future<String?> Function(String apiKey)? onValidateKey;
  final List<String> validatedKeys = [];

  @override
  Future<AnalysisOutcome> analyzePhoto(Uint8List originalBytes) async {
    if (onAnalyze != null) await onAnalyze!();
    return nextPhotoOutcome;
  }

  @override
  Future<Map<String, dynamic>?> textIntent(String prompt) async =>
      nextTextIntent;

  @override
  Future<String?> validateKey(String apiKey) {
    validatedKeys.add(apiKey);
    return onValidateKey?.call(apiKey) ?? Future.value(null);
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
}

class FakeSettings implements SettingsStore {
  @override
  String apiKey;
  @override
  String provider = 'gemini';
  @override
  bool isQuotaPaused = false;
  @override
  String model;
  @override
  int lookbackDays;
  @override
  String reportTime;
  @override
  bool watcherEnabled;
  @override
  String dietaryProfile;
  int updateCalls = 0;

  FakeSettings({
    this.apiKey = 'k',
    this.model = 'gemini-2.5-flash',
    this.lookbackDays = 2,
    this.reportTime = '21:00',
    this.watcherEnabled = false,
    this.dietaryProfile = '',
  });

  @override
  Future<void> update({
    String? apiKey,
    String? provider,
    String? model,
    int? lookbackDays,
    String? reportTime,
    bool? watcherEnabled,
    String? dietaryProfile,
  }) async {
    updateCalls++;
    if (apiKey != null) this.apiKey = apiKey;
    if (provider != null) this.provider = provider;
    if (model != null) this.model = model;
    if (lookbackDays != null) this.lookbackDays = lookbackDays;
    if (reportTime != null) this.reportTime = reportTime;
    if (watcherEnabled != null) this.watcherEnabled = watcherEnabled;
    if (dietaryProfile != null) this.dietaryProfile = dietaryProfile;
  }
}

class FakePicker implements RecentPhotoPicker {
  List<IntakePhoto> photos = const [];
  @override
  Future<List<IntakePhoto>> recentPhotos({int limit = 30}) async => photos;
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
}) =>
    UiServices(
      dao: dao ?? FakeDao(),
      analyzer: analyzer ?? FakeAnalyzer(),
      executor: executor ?? FakeExecutor(),
      settings: settings ?? FakeSettings(),
      picker: picker ?? FakePicker(),
      photoIntake: intake ?? FakeIntake(),
      reports: FakeReports(),
      requestPhotoPermission: () async => grantPhotoPermission,
    );
