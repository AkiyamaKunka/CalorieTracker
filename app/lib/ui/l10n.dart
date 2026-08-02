/// Localization access with an English fallback.
///
/// `context.l10n` instead of `AppLocalizations.of(context)`: widget tests
/// pump bare MaterialApps without delegates, and the ~15 existing suites
/// must keep passing untouched — a missing Localizations scope resolves to
/// English rather than throwing. Production always has the delegates, so
/// the fallback never fires there.
library;

import 'package:flutter/widgets.dart';

import '../l10n/app_localizations.dart';
import '../l10n/app_localizations_en.dart';

export '../l10n/app_localizations.dart' show AppLocalizations;

extension L10nX on BuildContext {
  AppLocalizations get l10n =>
      Localizations.of<AppLocalizations>(this, AppLocalizations) ??
      AppLocalizationsEn();
}

/// The switcher's choices: settings value → display name resolver.
/// 'system' follows the OS; explicit values force the app language.
Locale? localeForSetting(String appLanguage) => switch (appLanguage) {
      'en' => const Locale('en'),
      'zh' => const Locale('zh'),
      _ => null, // system
    };
