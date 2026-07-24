/// DI seam — the ONLY file that imports the concrete factory modules.
///
/// Workflow guard: if a factory import or signature drifts at integration
/// time, the integrator fixes THIS file alone.
library;

import 'dart:async' show unawaited;

import 'package:permission_handler/permission_handler.dart';

import '../core/contracts.dart';
import '../data/meals_dao_impl.dart';
import '../services/analyzer/provider_analyzers.dart';
import '../services/nl/executor.dart';
import '../services/photo/share_intake.dart';
import '../services/photo/watcher.dart';
import '../services/report/builders.dart';
import '../services/report/notifications.dart';
import '../services/settings/app_settings.dart';
import 'background_glue.dart';
import 'format.dart' show isoDate;
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
    final analyzer = createMultiProviderAnalyzer(settings);
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

    final ui = UiServices(
      dao: dao,
      analyzer: analyzer,
      executor: executor,
      photoIntake: photoIntake,
      reports: reports,
      settings: _AppSettingsStore(settings, notifier),
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
      // Launch catch-up over the FULL lookback window: change-notify only
      // fires while the process lives, and EMUI-class OS freezing kills it
      // minutes after backgrounding — photos taken since are picked up here.
      // Fire-and-forget: startup must not block on Gemini analyses. Key
      // guard: without a key nothing can succeed, so don't read photo bytes.
      if ((settings.activeApiKey ?? '').isNotEmpty &&
          !settings.isQuotaPaused) {
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

Future<bool> _requestPhotosPermission() async {
  final status = await Permission.photos.request();
  return status.isGranted || status.isLimited;
}

/// Adapts the persistent AppSettings onto the UI's SettingsStore, keeping
/// the background job and the daily-report schedule in lockstep with edits.
class _AppSettingsStore implements SettingsStore {
  final AppSettings _s;
  final ReportNotifier _notifier;
  _AppSettingsStore(this._s, this._notifier);

  @override
  String get apiKey => _s.activeApiKey ?? '';
  @override
  String get provider => _s.provider.name;
  @override
  bool get isQuotaPaused => _s.isQuotaPaused;
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
  Future<void> update({
    String? apiKey,
    String? provider,
    String? model,
    int? lookbackDays,
    String? reportTime,
    bool? watcherEnabled,
    String? dietaryProfile,
  }) async {
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
      };
    }
    if (model != null) {
      await switch (_s.provider) {
        AiProvider.gemini => _s.setModel(model),
        AiProvider.openai => _s.setOpenaiModel(model),
        AiProvider.anthropic => _s.setAnthropicModel(model),
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
