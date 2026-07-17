/// Local notifications for the report module (spec §5 delivery surface).
///
/// scheduleDaily takes the dependency-free route the port spec allows: the
/// timezone package is NOT a pubspec dependency, so instead of
/// zonedSchedule the notifier computes the next wall-clock occurrence with
/// plain DateTime, arms a Timer, and re-arms after each fire
/// (reschedule-after-fire pattern). Constraint: an in-process Timer does
/// not survive process death — the integrator re-calls scheduleDaily on app
/// launch with the persisted hh:mm setting. Exact delivery while the app is
/// dead would need zonedSchedule + the timezone dep, or platform alarms.
library;

import 'dart:async';

import 'package:flutter_local_notifications/flutter_local_notifications.dart';

/// Test seam: replaces the plugin's show() so scheduling logic is testable
/// without platform channels.
typedef NotificationPresenter = Future<void> Function(
    int id, String title, String body);

class ReportNotifier {
  ReportNotifier({
    FlutterLocalNotificationsPlugin? plugin,
    DateTime Function()? clock,
    NotificationPresenter? presenter,
    Future<String> Function()? dailyBody,
  })  : _plugin = plugin ?? FlutterLocalNotificationsPlugin(),
        _clock = clock ?? DateTime.now,
        // ignore: prefer_initializing_formals
        _presenter = presenter,
        // ignore: prefer_initializing_formals
        _dailyBody = dailyBody;

  /// Fixed id: re-firing replaces yesterday's report notification instead of
  /// stacking (one canonical daily report, spec §5.5).
  static const int dailyReportNotificationId = 9001;

  static const String _channelId = 'calorietracker_reports';

  final FlutterLocalNotificationsPlugin _plugin;
  final DateTime Function() _clock;
  final NotificationPresenter? _presenter;

  /// Body provider for the scheduled notification — the integrator passes a
  /// closure over ReportBuilder.dailyReport/todaySummary (contracts.dart:
  /// "the notification scheduler consumes dailyReport at the user's chosen
  /// time"). A throwing provider degrades to a generic body, never a missed
  /// notification.
  final Future<String> Function()? _dailyBody;

  Timer? _dailyTimer;
  ({int hour, int minute})? _dailyTime;
  int _nextMealCardId = 100; // distinct ids: meal cards stack, reports don't
  bool _initialized = false;

  DateTime? _nextDailyFire;

  /// The armed daily slot (for UI display and tests); null when unscheduled.
  DateTime? get nextDailyFire => _nextDailyFire;

  Future<void> init() async {
    if (_presenter != null || _initialized) return;
    // DarwinInitializationSettings defaults request alert/badge/sound
    // permission during initialize on iOS/macOS.
    const settings = InitializationSettings(
      android: AndroidInitializationSettings('@mipmap/ic_launcher'),
      iOS: DarwinInitializationSettings(),
      macOS: DarwinInitializationSettings(),
    );
    await _plugin.initialize(settings: settings);
    try {
      // Android 13+ runtime grant; a denial means silent no-shows, so a
      // failure here is non-fatal.
      await _plugin
          .resolvePlatformSpecificImplementation<
              AndroidFlutterLocalNotificationsPlugin>()
          ?.requestNotificationsPermission();
    } catch (_) {
      // Older plugin/platform combinations: proceed without the grant.
    }
    _initialized = true;
  }

  /// Parse a 24-hour "HH:mm" string. Throws [ArgumentError] on anything else.
  static ({int hour, int minute}) parseHhmm(String hhmm) {
    final match = RegExp(r'^(\d{1,2}):(\d{2})$').firstMatch(hhmm.trim());
    if (match != null) {
      final hour = int.parse(match.group(1)!);
      final minute = int.parse(match.group(2)!);
      if (hour <= 23 && minute <= 59) return (hour: hour, minute: minute);
    }
    throw ArgumentError.value(hhmm, 'hhmm', 'expected 24h "HH:mm"');
  }

  /// Next wall-clock occurrence of [time] strictly after [now]; a slot equal
  /// to now schedules tomorrow. The component constructor normalizes day
  /// overflow, keeping DST days correct at wall-clock level.
  static DateTime nextDailyOccurrence(
      ({int hour, int minute}) time, DateTime now) {
    var candidate =
        DateTime(now.year, now.month, now.day, time.hour, time.minute);
    if (!candidate.isAfter(now)) {
      candidate =
          DateTime(now.year, now.month, now.day + 1, time.hour, time.minute);
    }
    return candidate;
  }

  /// Arm (or re-arm, replacing any previous schedule) the daily report
  /// notification at [hhmm] local wall-clock time.
  Future<void> scheduleDaily(String hhmm) async {
    // parseHhmm throws before any state changes: an invalid hh:mm leaves an
    // existing schedule running.
    _dailyTime = parseHhmm(hhmm);
    _armDailyTimer();
  }

  void cancelDaily() {
    _dailyTimer?.cancel();
    _dailyTimer = null;
    _dailyTime = null;
    _nextDailyFire = null;
  }

  void _armDailyTimer() {
    final time = _dailyTime;
    if (time == null) return;
    _dailyTimer?.cancel();
    final now = _clock();
    final next = nextDailyOccurrence(time, now);
    _nextDailyFire = next;
    _dailyTimer = Timer(next.difference(now), () {
      // Reschedule-after-fire: re-arm BEFORE presenting so a throwing body
      // can never kill tomorrow's schedule.
      _armDailyTimer();
      unawaited(_fireDaily());
    });
  }

  Future<void> _fireDaily() async {
    var body = 'Your daily calorie report is ready.';
    final provider = _dailyBody;
    if (provider != null) {
      try {
        body = await provider();
      } catch (_) {
        // Keep the fallback body; the schedule already re-armed.
      }
    }
    await _present(dailyReportNotificationId, '📊 Daily Calorie Report', body);
  }

  /// Meal card for watcher-logged meals (spec §6 auto intake surfaces its
  /// §5.4 result card as a notification). Ids increment so multiple
  /// auto-logged meals stack instead of overwriting each other.
  Future<void> showMealCard(String title, String body) =>
      _present(_nextMealCardId++, title, body);

  Future<void> _present(int id, String title, String body) async {
    final presenter = _presenter;
    if (presenter != null) return presenter(id, title, body);
    const details = NotificationDetails(
      android: AndroidNotificationDetails(
        _channelId,
        'Reports & meals',
        channelDescription: 'Daily calorie reports and auto-logged meal cards',
        importance: Importance.defaultImportance,
        priority: Priority.defaultPriority,
      ),
      iOS: DarwinNotificationDetails(),
      macOS: DarwinNotificationDetails(),
    );
    await _plugin.show(
        id: id, title: title, body: body, notificationDetails: details);
  }
}
