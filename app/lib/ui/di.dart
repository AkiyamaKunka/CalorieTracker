/// DI seam — the ONLY file that imports the concrete factory modules.
///
/// Workflow guard: if a factory import or signature drifts at integration
/// time, the integrator fixes THIS file alone.
library;

import 'package:permission_handler/permission_handler.dart';

import '../core/contracts.dart';
import '../data/meals_dao_impl.dart';
import '../services/analyzer/gemini_analyzer.dart';
import '../services/nl/executor.dart';
import '../services/photo/share_intake.dart';
import '../services/photo/watcher.dart';
import '../services/report/builders.dart';
import '../services/settings/app_settings.dart';
import 'photo_pipeline.dart';
import 'services.dart';

/// Singleton wiring of the workflow-fixed factories, in the spec'd startup
/// order: settings → dao → reclaimStaleProcessing (spec §2.3 launch sweep)
/// → services.
class AppServices {
  final UiServices ui;
  final PhotoPipeline pipeline;
  final AppSettings settings;

  AppServices._(this.ui, this.pipeline, this.settings);

  static AppServices? _instance;
  static AppServices get instance => _instance!;

  static Future<AppServices> init({void Function(String)? notify}) async {
    if (_instance != null) return _instance!;

    final settings = await AppSettings.load();
    final MealsDao dao = await createMealsDao();
    // Any 'processing' ledger row at launch is a crashed run — reclaim it
    // before services start (spec §2.3 single-process simplification).
    await dao.reclaimStaleProcessing();
    final analyzer = createAnalyzer(settings);
    final executor = createExecutor(dao, analyzer, settings);
    // The photo module reads lookbackDays through the live settings object,
    // so slider edits apply to the next scan without rewiring (spec §6.4).
    final photoIntake = createPhotoIntake(settings);
    final shareIntake = createShareIntake(settings);
    final ReportBuilder reports = createReportBuilder(dao);

    final ui = UiServices(
      dao: dao,
      analyzer: analyzer,
      executor: executor,
      photoIntake: photoIntake,
      reports: reports,
      settings: _AppSettingsStore(settings),
      picker: _ShareIntakePicker(shareIntake),
      requestPhotoPermission: _requestPhotosPermission,
    );

    // Photo pipeline glue (spec §2.3/§3/§6): intake streams → md5 → reserve
    // → analyze → save/skip/fail, notification-style snackbar on save.
    final pipeline =
        PhotoPipeline(dao: dao, analyzer: analyzer, notify: notify);
    pipeline.bind(photoIntake); // automated watch (deliberate=false)
    pipeline.bindStream(shareIntake.photos()); // share sheet (deliberate)
    if (settings.watcherEnabled) {
      await photoIntake.start();
    }

    return _instance = AppServices._(ui, pipeline, settings);
  }
}

Future<bool> _requestPhotosPermission() async {
  final status = await Permission.photos.request();
  return status.isGranted || status.isLimited;
}

/// Adapts the persistent AppSettings onto the UI's SettingsStore.
class _AppSettingsStore implements SettingsStore {
  final AppSettings _s;
  _AppSettingsStore(this._s);

  @override
  String get apiKey => _s.geminiApiKey ?? '';
  @override
  String get model => _s.model;
  @override
  int get lookbackDays => _s.lookbackDays;
  @override
  String get reportTime => _s.reportTime;
  @override
  bool get watcherEnabled => _s.watcherEnabled;
  @override
  String get dietaryProfile => _s.dietaryProfile ?? '';

  @override
  Future<void> update({
    String? apiKey,
    String? model,
    int? lookbackDays,
    String? reportTime,
    bool? watcherEnabled,
    String? dietaryProfile,
  }) async {
    if (apiKey != null) await _s.setGeminiApiKey(apiKey);
    if (model != null) await _s.setModel(model);
    if (lookbackDays != null) await _s.setLookbackDays(lookbackDays);
    if (reportTime != null) {
      try {
        await _s.setReportTime(reportTime); // validates HH:mm, throws on junk
      } on ArgumentError {
        // UI always sends zero-padded HH:mm; a junk value keeps the default.
      }
    }
    if (watcherEnabled != null) await _s.setWatcherEnabled(watcherEnabled);
    if (dietaryProfile != null) await _s.setDietaryProfile(dietaryProfile);
  }
}

/// The Add-flow grid source: the photo module's pickFromRecent already
/// returns ORIGINAL bytes with deliberate=true (spec §2.3/§6.2).
class _ShareIntakePicker implements RecentPhotoPicker {
  final ShareIntake _share;
  _ShareIntakePicker(this._share);

  @override
  Future<List<IntakePhoto>> recentPhotos({int limit = 30}) =>
      _share.pickFromRecent(limit);
}
