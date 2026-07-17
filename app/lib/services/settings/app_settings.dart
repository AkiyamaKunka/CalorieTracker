/// User-editable settings + persistence (spec §8 knobs).
///
/// Non-secret values live in shared_preferences; the Gemini API key lives in
/// flutter_secure_storage (spec §8: "user-supplied, stored in secure storage").
/// The analyzer's daily-quota pause latch (spec §3.3) is persisted here too so
/// it survives restarts. A simple [ChangeNotifier] so UI can listen.
library;

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Narrow seam over the secure-storage plugin so tests can substitute an
/// in-memory store (the platform channel is unavailable in unit tests).
abstract class SecureKeyStore {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
  Future<void> delete(String key);
}

/// Production [SecureKeyStore] backed by flutter_secure_storage.
class FlutterSecureKeyStore implements SecureKeyStore {
  const FlutterSecureKeyStore([this._storage = const FlutterSecureStorage()]);
  final FlutterSecureStorage _storage;

  @override
  Future<String?> read(String key) => _storage.read(key: key);
  @override
  Future<void> write(String key, String value) =>
      _storage.write(key: key, value: value);
  @override
  Future<void> delete(String key) => _storage.delete(key: key);
}

class AppSettings extends ChangeNotifier {
  AppSettings._(this._prefs, this._keys);

  final SharedPreferences _prefs;
  final SecureKeyStore _keys;

  // Secure-storage key (never written to shared_preferences).
  static const String _kApiKey = 'gemini_api_key';

  // shared_preferences keys.
  static const String _kModel = 'settings.model';
  static const String _kLookbackDays = 'settings.lookback_days';
  static const String _kReportTime = 'settings.report_time';
  static const String _kWatcherEnabled = 'settings.watcher_enabled';
  static const String _kDietaryProfile = 'settings.dietary_profile';
  static const String _kQuotaPauseUntil = 'settings.quota_pause_until';

  /// Spec §8: Gemini model default (config.py:26).
  static const String defaultModel = 'gemini-2.5-flash';

  /// Spec §8: SYNC_LOOKBACK_DAYS default 2, clamp 1–30 (upload_photo.py:68).
  static const int defaultLookbackDays = 2;
  static const int minLookbackDays = 1;
  static const int maxLookbackDays = 30;

  static const String defaultReportTime = '21:30';
  static final RegExp _reportTimeRe = RegExp(r'^([01]?\d|2[0-3]):[0-5]\d$');

  String? _geminiApiKey;
  String _model = defaultModel;
  int _lookbackDays = defaultLookbackDays;
  String _reportTime = defaultReportTime;
  bool _watcherEnabled = false;
  String? _dietaryProfile;
  DateTime? _quotaPauseUntil;

  static Future<AppSettings> load(
      {SharedPreferences? prefs, SecureKeyStore? keyStore}) async {
    final p = prefs ?? await SharedPreferences.getInstance();
    final k = keyStore ?? const FlutterSecureKeyStore();
    final s = AppSettings._(p, k);
    final storedKey = (await k.read(_kApiKey))?.trim();
    s._geminiApiKey = (storedKey == null || storedKey.isEmpty) ? null : storedKey;
    final model = (p.getString(_kModel) ?? '').trim();
    s._model = model.isEmpty ? defaultModel : model;
    s._lookbackDays = (p.getInt(_kLookbackDays) ?? defaultLookbackDays)
        .clamp(minLookbackDays, maxLookbackDays);
    final rt = p.getString(_kReportTime) ?? defaultReportTime;
    s._reportTime = _reportTimeRe.hasMatch(rt) ? rt : defaultReportTime;
    s._watcherEnabled = p.getBool(_kWatcherEnabled) ?? false;
    final profile = (p.getString(_kDietaryProfile) ?? '').trim();
    s._dietaryProfile = profile.isEmpty ? null : profile;
    s._quotaPauseUntil = DateTime.tryParse(p.getString(_kQuotaPauseUntil) ?? '');
    return s;
  }

  /// User's Gemini API key; null until the user supplies one.
  String? get geminiApiKey => _geminiApiKey;

  /// Persists to secure storage only. Null/blank clears the key.
  Future<void> setGeminiApiKey(String? value) async {
    final v = (value ?? '').trim();
    if (v.isEmpty) {
      _geminiApiKey = null;
      await _keys.delete(_kApiKey);
    } else {
      _geminiApiKey = v;
      await _keys.write(_kApiKey, v);
    }
    notifyListeners();
  }

  String get model => _model;

  /// Blank resets to [defaultModel] (spec §8: user-editable model string).
  Future<void> setModel(String value) async {
    final v = value.trim();
    _model = v.isEmpty ? defaultModel : v;
    await _prefs.setString(_kModel, _model);
    notifyListeners();
  }

  /// Backfill-scan window, spec §6.4 / §8 (SYNC_LOOKBACK_DAYS, clamp 1–30).
  int get lookbackDays => _lookbackDays;

  Future<void> setLookbackDays(int value) async {
    _lookbackDays = value.clamp(minLookbackDays, maxLookbackDays);
    await _prefs.setInt(_kLookbackDays, _lookbackDays);
    notifyListeners();
  }

  /// Local time (24h "HH:mm") for the daily-report notification (spec §5.5).
  String get reportTime => _reportTime;

  Future<void> setReportTime(String value) async {
    final v = value.trim();
    if (!_reportTimeRe.hasMatch(v)) {
      throw ArgumentError.value(value, 'reportTime', 'expected HH:mm (24h)');
    }
    _reportTime = v;
    await _prefs.setString(_kReportTime, v);
    notifyListeners();
  }

  /// Spec §6 auto intake toggle — defaults false until permissions granted.
  bool get watcherEnabled => _watcherEnabled;

  Future<void> setWatcherEnabled(bool value) async {
    _watcherEnabled = value;
    await _prefs.setBool(_kWatcherEnabled, value);
    notifyListeners();
  }

  /// Spec §1.3 dietary-profile text; null when unset/blank.
  String? get dietaryProfile => _dietaryProfile;

  Future<void> setDietaryProfile(String? value) async {
    final v = (value ?? '').trim();
    _dietaryProfile = v.isEmpty ? null : v;
    if (_dietaryProfile == null) {
      await _prefs.remove(_kDietaryProfile);
    } else {
      await _prefs.setString(_kDietaryProfile, _dietaryProfile!);
    }
    notifyListeners();
  }

  /// Daily-quota pause latch (spec §3.3): while now < this, photo analysis is
  /// skipped and NL requests are refused. Null = not paused.
  DateTime? get quotaPauseUntil => _quotaPauseUntil;

  Future<void> setQuotaPauseUntil(DateTime? value) async {
    _quotaPauseUntil = value;
    if (value == null) {
      await _prefs.remove(_kQuotaPauseUntil);
    } else {
      await _prefs.setString(_kQuotaPauseUntil, value.toIso8601String());
    }
    notifyListeners();
  }

  bool get isQuotaPaused =>
      _quotaPauseUntil != null && DateTime.now().isBefore(_quotaPauseUntil!);
}
