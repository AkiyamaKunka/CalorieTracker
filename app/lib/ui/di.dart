/// DI seam — the ONLY file that imports the concrete factory modules.
///
/// Workflow guard: if a factory import or signature drifts at integration
/// time, the integrator fixes THIS file alone.
library;

import 'dart:async' show unawaited;
import 'dart:typed_data';

import 'package:flutter/foundation.dart' show compute;

import 'package:permission_handler/permission_handler.dart';

import '../core/contracts.dart';
import '../data/meals_dao_impl.dart';
import '../services/analyzer/provider_analyzers.dart'
    show ServerAnalyzer, createMultiProviderAnalyzer;
import '../services/nl/executor.dart';
import '../services/garmin_client.dart';
import '../services/photo/coverage.dart';
import '../services/photo/filename_dates.dart' show deriveCapturedAt;
import '../services/photo/photo_hash.dart' show originalBytesMd5;
import '../services/photo/photo_library.dart';
import '../services/photo/share_intake.dart';
import '../services/photo/watcher.dart';
import '../services/report/builders.dart';
import '../services/report/notifications.dart';
import '../services/settings/app_settings.dart';
import 'background_glue.dart';
import 'format.dart' show isoDate;
import 'meal_thumbs.dart';
import 'photo_pipeline.dart';
import 'refresh_signal.dart';
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
    final analyzer = createMultiProviderAnalyzer(settings);
    // A dedicated ServerAnalyzer instance for the OAuth re-connect calls:
    // they exist regardless of the currently selected provider (the user
    // may re-connect Claude while temporarily on Gemini).
    final serverAnalyzer = ServerAnalyzer(settings);
    final executor = createExecutor(dao, analyzer, settings);
    // The photo module reads lookbackDays through the live settings object,
    // so slider edits apply to the next scan without rewiring (spec §6.4).
    final photoIntake = createPhotoIntake(settings);
    final shareIntake = createShareIntake(settings);
    final ReportBuilder reports = createReportBuilder(dao);

    // Notification surface (spec §5.5/§6): init() also performs the runtime
    // permission request (Android 13+ / iOS) — without it every meal card
    // and daily report is silently dropped. The in-process daily Timer dies
    // with the process, so re-arm it on every launch (notifications.dart
    // integrator contract). MUST be fire-and-forget: init() resolves only
    // when the user answers the permission dialog, and awaiting that here
    // blocks the first frame indefinitely (found by the iOS E2E hanging
    // 15 minutes at launch).
    // dailyBody gets the ARMED SLOT's date: a Timer delivered late (after
    // overnight suspension) must still report the day it was scheduled
    // for, not the fresh morning's near-empty totals.
    final notifier = ReportNotifier(
        dailyBody: (slotDate) => reports.dailyReport(isoDate(slotDate)));
    unawaited(() async {
      try {
        await notifier.init();
        await notifier.scheduleDaily(settings.reportTime);
      } catch (_) {
        // Permission denial / corrupt hh:mm must never break startup.
      }
    }());

    // Photo pipeline glue (spec §2.3/§3/§6): intake streams → md5 → reserve
    // → analyze → save/skip/fail, notification-style snackbar on save.
    final pipeline = PhotoPipeline(
        dao: dao,
        analyzer: analyzer,
        notify: notify,
        // Open screens re-query instead of showing pre-save data.
        onMealSaved: signalMealsChanged);

    final photoLibrary = PhotoManagerLibrary();
    // PhotoManager, NOT permission_handler: on Android <=12 the latter's
    // Permission.photos resolves to READ_MEDIA_IMAGES (SDK 33+ only) and
    // auto-DENIES with no dialog, dead-ending both photo flows on the
    // cheap end of the friend cohort. PhotoManager.requestPermissionExtend
    // handles every version and reports limited vs full.
    Future<bool> requestPhotos() => photoLibrary.requestPermission();
    final ui = UiServices(
      dao: dao,
      analyzer: analyzer,
      executor: executor,
      photoIntake: photoIntake,
      reports: reports,
      settings: _AppSettingsStore(settings, notifier),
      settingsChanges: settings,
      garminDaily: makeGarminDailyFetch(settings),
      picker: _ShareIntakePicker(photoLibrary),
      requestPhotoPermission: requestPhotos,
      thumbs: MealThumbResolver(dao: dao, library: photoLibrary),
      coverage: CoverageAuditor(
          library: photoLibrary,
          dao: dao,
          // compute(): the audit hashes up to 2000 originals back-to-back —
          // synchronous md5 on the UI isolate would freeze scrolling for
          // the whole sweep.
          hasher: (bytes) => compute(originalBytesMd5, bytes)),
      // The audit's log/retry actions share the pipeline's global FIFO —
      // enqueue() chains onto the tail; process() would race it.
      processPhoto: pipeline.enqueue,
      photoLibrary: photoLibrary,
      startClaudeAuth: serverAnalyzer.startClaudeAuth,
      completeClaudeAuth: serverAnalyzer.completeClaudeAuth,
      openSystemSettings: () async {
        await openAppSettings(); // permission_handler
      },
    );
    pipeline.bind(photoIntake); // automated watch (deliberate=false)
    pipeline.bindStream(shareIntake.photos()); // share sheet (deliberate)
    if (settings.watcherEnabled) {
      await photoIntake.start();
      // Launch catch-up over the FULL lookback window: change-notify only
      // fires while the process lives, and EMUI-class OS freezing kills it
      // minutes after backgrounding — photos taken since are picked up here.
      // Fire-and-forget: startup must not block on Gemini analyses. Key
      // guard: without a key nothing can succeed, so don't read photo bytes.
      if (settings.canAnalyze) {
        unawaited(photoIntake.backfillScan().then((_) {},
            onError: (Object e) {
          // Truncation must not vanish silently: >2000 window images means
          // the catch-up cannot promise full coverage.
          if (e is BackfillWindowTruncated) {
            notify?.call('Photo library backlog is very large — some older '
                'photos may need to be added manually.');
          }
        }));
      }
    }
    // Reconcile the periodic WorkManager backfill (background_glue) with the
    // persisted toggle so it also runs while the app is closed.
    await syncBackgroundScan(settings);

    return _instance = AppServices._(ui, pipeline, settings);
  }
}

/// Test seam for the adapter below: it is the ONLY production code that
/// maps the UI's provider-scoped apiKey/model onto AppSettings' seven
/// slots, and every widget test uses a fake that merely SIMULATES that
/// routing — so a real mis-route would have stayed green (review
/// 2026-07-31). [notifier] is optional here; the daily-report reschedule
/// is exercised by the report suite.
SettingsStore createSettingsStore(AppSettings settings,
        {ReportNotifier? notifier}) =>
    _AppSettingsStore(settings, notifier ?? ReportNotifier(dailyBody: null));

/// Adapts the persistent AppSettings onto the UI's SettingsStore, keeping
/// the background job and the daily-report schedule in lockstep with edits.
class _AppSettingsStore implements SettingsStore {
  final AppSettings _s;
  final ReportNotifier _notifier;
  _AppSettingsStore(this._s, this._notifier);

  @override
  String get apiKey => _s.activeApiKey ?? '';
  @override
  bool get canAnalyze => _s.canAnalyze;
  @override
  String get provider => _s.provider.name;
  @override
  bool get isQuotaPaused => _s.isQuotaPaused;
  @override
  DateTime? get quotaPauseUntil => _s.quotaPauseUntil;
  @override
  String get model => _s.activeModel;
  @override
  int get lookbackDays => _s.lookbackDays;
  @override
  String get reportTime => _s.reportTime;
  @override
  bool get watcherEnabled => _s.watcherEnabled;
  @override
  String get dietaryProfile => _s.dietaryProfile ?? '';
  @override
  String get serverBaseUrl => _s.serverBaseUrl;
  @override
  String get serverBackend => _s.serverBackend;
  @override
  String get appLanguage => _s.appLanguage;

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
    if (appLanguage != null) await _s.setAppLanguage(appLanguage);
    if (provider != null) {
      await _s.setProvider(AiProvider.values
          .firstWhere((v) => v.name == provider, orElse: () => _s.provider));
    }
    // apiKey/model always target the ACTIVE provider (the UI's key and
    // model fields are provider-scoped by design).
    if (apiKey != null) {
      await switch (_s.provider) {
        AiProvider.gemini => _s.setGeminiApiKey(apiKey),
        AiProvider.openai => _s.setOpenaiApiKey(apiKey),
        AiProvider.anthropic => _s.setAnthropicApiKey(apiKey),
        AiProvider.server => _s.setServerApiKey(apiKey),
        AiProvider.qwen => _s.setQwenApiKey(apiKey),
        AiProvider.doubao => _s.setDoubaoApiKey(apiKey),
        AiProvider.glm => _s.setGlmApiKey(apiKey),
      };
    }
    if (model != null) {
      await switch (_s.provider) {
        AiProvider.gemini => _s.setModel(model),
        AiProvider.openai => _s.setOpenaiModel(model),
        AiProvider.anthropic => _s.setAnthropicModel(model),
        // The server chooses its own model; the field is hidden in the UI.
        AiProvider.server => Future<void>.value(),
        AiProvider.qwen => _s.setQwenModel(model),
        AiProvider.doubao => _s.setDoubaoModel(model),
        AiProvider.glm => _s.setGlmModel(model),
      };
    }
    if (lookbackDays != null) {
      await _s.setLookbackDays(lookbackDays);
      await syncBackgroundScan(_s);
    }
    if (reportTime != null) {
      try {
        await _s.setReportTime(reportTime); // validates HH:mm, throws on junk
        await _notifier.scheduleDaily(_s.reportTime); // re-arm on the new slot
      } on ArgumentError {
        // UI always sends zero-padded HH:mm; a junk value keeps the default.
      }
    }
    if (watcherEnabled != null) {
      await _s.setWatcherEnabled(watcherEnabled);
      await syncBackgroundScan(_s); // register/cancel the periodic job
    }
    if (dietaryProfile != null) await _s.setDietaryProfile(dietaryProfile);
    if (serverBaseUrl != null) await _s.setServerBaseUrl(serverBaseUrl);
    if (serverBackend != null) await _s.setServerBackend(serverBackend);
  }
}

/// The Add-flow grid source: list assets, thumbnail per visible cell,
/// original bytes only for the photo the user taps.
class _ShareIntakePicker implements RecentPhotoPicker {
  final PhotoLibrary _library;
  _ShareIntakePicker(this._library);

  @override
  Future<List<RecentAsset>> recentAssets({int limit = 30}) async {
    if (!await _library.requestPermission()) return const [];
    final assets = await _library.recentImages(limit);
    return [
      for (final a in assets.take(limit))
        RecentAsset(a.id, await a.fileName(), a.createDateTime),
    ];
  }

  @override
  Future<Uint8List?> thumbnail(String assetId) =>
      _library.thumbnailByAssetId(assetId);

  @override
  Future<IntakePhoto?> loadOriginal(RecentAsset asset) async {
    final bytes = await _library.originBytesByAssetId(asset.id);
    if (bytes == null || bytes.isEmpty) return null;
    // §8 25 MB cap, as every other intake path enforces: the eager
    // pickFromRecent skipped oversize photos, and the lazy rewrite lost
    // that guard — a 40 MP original would hit the analyzer's normalizer
    // and the base64 upload on a phone that cannot afford either.
    if (bytes.length > maxPhotoBytes) return null;
    // §6.3 dating from the asset's own metadata, same rule the eager path
    // applied; deliberate=true (user-picked, spec §2.3).
    return IntakePhoto(bytes, asset.id, asset.fileName,
        capturedAt: deriveCapturedAt(
            fileName: asset.fileName, assetCreateDate: asset.createdAt),
        deliberate: true);
  }
}
