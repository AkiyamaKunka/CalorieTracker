import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:intl/intl.dart' as intl;

import 'app_localizations_en.dart';
import 'app_localizations_zh.dart';

// ignore_for_file: type=lint

/// Callers can lookup localized strings with an instance of AppLocalizations
/// returned by `AppLocalizations.of(context)`.
///
/// Applications need to include `AppLocalizations.delegate()` in their app's
/// `localizationDelegates` list, and the locales they support in the app's
/// `supportedLocales` list. For example:
///
/// ```dart
/// import 'l10n/app_localizations.dart';
///
/// return MaterialApp(
///   localizationsDelegates: AppLocalizations.localizationsDelegates,
///   supportedLocales: AppLocalizations.supportedLocales,
///   home: MyApplicationHome(),
/// );
/// ```
///
/// ## Update pubspec.yaml
///
/// Please make sure to update your pubspec.yaml to include the following
/// packages:
///
/// ```yaml
/// dependencies:
///   # Internationalization support.
///   flutter_localizations:
///     sdk: flutter
///   intl: any # Use the pinned version from flutter_localizations
///
///   # Rest of dependencies
/// ```
///
/// ## iOS Applications
///
/// iOS applications define key application metadata, including supported
/// locales, in an Info.plist file that is built into the application bundle.
/// To configure the locales supported by your app, you’ll need to edit this
/// file.
///
/// First, open your project’s ios/Runner.xcworkspace Xcode workspace file.
/// Then, in the Project Navigator, open the Info.plist file under the Runner
/// project’s Runner folder.
///
/// Next, select the Information Property List item, select Add Item from the
/// Editor menu, then select Localizations from the pop-up menu.
///
/// Select and expand the newly-created Localizations item then, for each
/// locale your application supports, add a new item and select the locale
/// you wish to add from the pop-up menu in the Value field. This list should
/// be consistent with the languages listed in the AppLocalizations.supportedLocales
/// property.
abstract class AppLocalizations {
  AppLocalizations(String locale)
    : localeName = intl.Intl.canonicalizedLocale(locale.toString());

  final String localeName;

  static AppLocalizations of(BuildContext context) {
    return Localizations.of<AppLocalizations>(context, AppLocalizations)!;
  }

  static const LocalizationsDelegate<AppLocalizations> delegate =
      _AppLocalizationsDelegate();

  /// A list of this localizations delegate along with the default localizations
  /// delegates.
  ///
  /// Returns a list of localizations delegates containing this delegate along with
  /// GlobalMaterialLocalizations.delegate, GlobalCupertinoLocalizations.delegate,
  /// and GlobalWidgetsLocalizations.delegate.
  ///
  /// Additional delegates can be added by appending to this list in
  /// MaterialApp. This list does not have to be used at all if a custom list
  /// of delegates is preferred or required.
  static const List<LocalizationsDelegate<dynamic>> localizationsDelegates =
      <LocalizationsDelegate<dynamic>>[
        delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
      ];

  /// A list of this localizations delegate's supported locales.
  static const List<Locale> supportedLocales = <Locale>[
    Locale('en'),
    Locale('zh'),
  ];

  /// No description provided for @tabToday.
  ///
  /// In en, this message translates to:
  /// **'Today'**
  String get tabToday;

  /// No description provided for @tabHistory.
  ///
  /// In en, this message translates to:
  /// **'History'**
  String get tabHistory;

  /// No description provided for @tabBody.
  ///
  /// In en, this message translates to:
  /// **'Body'**
  String get tabBody;

  /// No description provided for @tabSettings.
  ///
  /// In en, this message translates to:
  /// **'Settings'**
  String get tabSettings;

  /// No description provided for @kcalAmount.
  ///
  /// In en, this message translates to:
  /// **'{kcal} kcal'**
  String kcalAmount(String kcal);

  /// No description provided for @mealsToday.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{1 meal today} other{{count} meals today}}'**
  String mealsToday(int count);

  /// No description provided for @rowTypical.
  ///
  /// In en, this message translates to:
  /// **'Typical'**
  String get rowTypical;

  /// No description provided for @rowBurn.
  ///
  /// In en, this message translates to:
  /// **'Burn'**
  String get rowBurn;

  /// No description provided for @rowEaten.
  ///
  /// In en, this message translates to:
  /// **'Eaten'**
  String get rowEaten;

  /// No description provided for @rowResultLeft.
  ///
  /// In en, this message translates to:
  /// **'= Left'**
  String get rowResultLeft;

  /// No description provided for @rowResultOver.
  ///
  /// In en, this message translates to:
  /// **'= Above typical'**
  String get rowResultOver;

  /// No description provided for @ringHeadroom.
  ///
  /// In en, this message translates to:
  /// **'headroom'**
  String get ringHeadroom;

  /// No description provided for @ringLeftToday.
  ///
  /// In en, this message translates to:
  /// **'left today'**
  String get ringLeftToday;

  /// No description provided for @ringAboveTypical.
  ///
  /// In en, this message translates to:
  /// **'above typical'**
  String get ringAboveTypical;

  /// No description provided for @ringKcalToday.
  ///
  /// In en, this message translates to:
  /// **'kcal today'**
  String get ringKcalToday;

  /// No description provided for @todayEmptyTitle.
  ///
  /// In en, this message translates to:
  /// **'No meals logged yet today.'**
  String get todayEmptyTitle;

  /// No description provided for @todayEmptyHint.
  ///
  /// In en, this message translates to:
  /// **'Tap \"Log\" below to add one from a photo or a description — or turn on \"Watch camera roll\" in Settings and new food photos log themselves.'**
  String get todayEmptyHint;

  /// No description provided for @fabLog.
  ///
  /// In en, this message translates to:
  /// **'Log'**
  String get fabLog;

  /// No description provided for @shareDayTooltip.
  ///
  /// In en, this message translates to:
  /// **'Share today as an image'**
  String get shareDayTooltip;

  /// No description provided for @shareDayFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not build the image: {error}'**
  String shareDayFailed(String error);

  /// No description provided for @historyAverage.
  ///
  /// In en, this message translates to:
  /// **'Average: ~{kcal} kcal / day'**
  String historyAverage(String kcal);

  /// No description provided for @historyNoMeals.
  ///
  /// In en, this message translates to:
  /// **'no meals logged'**
  String get historyNoMeals;

  /// No description provided for @historyEmpty.
  ///
  /// In en, this message translates to:
  /// **'No meals logged in the past {days} days.'**
  String historyEmpty(int days);

  /// No description provided for @retry.
  ///
  /// In en, this message translates to:
  /// **'Retry'**
  String get retry;

  /// No description provided for @bodyWeightHeader.
  ///
  /// In en, this message translates to:
  /// **'Weight'**
  String get bodyWeightHeader;

  /// No description provided for @bodyMeasurementsHeader.
  ///
  /// In en, this message translates to:
  /// **'Measurements'**
  String get bodyMeasurementsHeader;

  /// No description provided for @bodyHistoryHeader.
  ///
  /// In en, this message translates to:
  /// **'History'**
  String get bodyHistoryHeader;

  /// No description provided for @bodyOnDate.
  ///
  /// In en, this message translates to:
  /// **'on {date}'**
  String bodyOnDate(String date);

  /// No description provided for @bodyNoChange.
  ///
  /// In en, this message translates to:
  /// **'no change'**
  String get bodyNoChange;

  /// No description provided for @bodyWaist.
  ///
  /// In en, this message translates to:
  /// **'Waist'**
  String get bodyWaist;

  /// No description provided for @bodyChest.
  ///
  /// In en, this message translates to:
  /// **'Chest'**
  String get bodyChest;

  /// No description provided for @bodyHip.
  ///
  /// In en, this message translates to:
  /// **'Hip'**
  String get bodyHip;

  /// No description provided for @bodyEmptyTitle.
  ///
  /// In en, this message translates to:
  /// **'No body data yet.'**
  String get bodyEmptyTitle;

  /// No description provided for @bodyEmptyHint.
  ///
  /// In en, this message translates to:
  /// **'Tap Log to record your weight or your waist, chest and hip measurements. Weight logged by chat (\"I weigh 81.6 kg\") lands here too.'**
  String get bodyEmptyHint;

  /// No description provided for @bodySheetLogTitle.
  ///
  /// In en, this message translates to:
  /// **'Log body · {date}'**
  String bodySheetLogTitle(String date);

  /// No description provided for @bodySheetEditTitle.
  ///
  /// In en, this message translates to:
  /// **'Edit {date}'**
  String bodySheetEditTitle(String date);

  /// No description provided for @bodySheetHint.
  ///
  /// In en, this message translates to:
  /// **'Leave anything you did not measure empty.'**
  String get bodySheetHint;

  /// No description provided for @bodyFieldWeight.
  ///
  /// In en, this message translates to:
  /// **'Weight'**
  String get bodyFieldWeight;

  /// No description provided for @save.
  ///
  /// In en, this message translates to:
  /// **'Save'**
  String get save;

  /// No description provided for @saving.
  ///
  /// In en, this message translates to:
  /// **'Saving…'**
  String get saving;

  /// No description provided for @bodyErrNotNumber.
  ///
  /// In en, this message translates to:
  /// **'{label}: \"{raw}\" is not a number.'**
  String bodyErrNotNumber(String label, String raw);

  /// No description provided for @bodyErrBounds.
  ///
  /// In en, this message translates to:
  /// **'{label} must be between {min} and {max}.'**
  String bodyErrBounds(String label, String min, String max);

  /// No description provided for @bodyErrEmpty.
  ///
  /// In en, this message translates to:
  /// **'Enter at least one value.'**
  String get bodyErrEmpty;

  /// No description provided for @bodyDeleteTitle.
  ///
  /// In en, this message translates to:
  /// **'Delete {date}?'**
  String bodyDeleteTitle(String date);

  /// No description provided for @bodyDeleteBody.
  ///
  /// In en, this message translates to:
  /// **'Removes the {what} recorded for this day.'**
  String bodyDeleteBody(String what);

  /// No description provided for @bodyDeleteWeight.
  ///
  /// In en, this message translates to:
  /// **'weight'**
  String get bodyDeleteWeight;

  /// No description provided for @bodyDeleteMeasurements.
  ///
  /// In en, this message translates to:
  /// **'measurements'**
  String get bodyDeleteMeasurements;

  /// No description provided for @bodyDeleteBoth.
  ///
  /// In en, this message translates to:
  /// **'weight and measurements'**
  String get bodyDeleteBoth;

  /// No description provided for @cancel.
  ///
  /// In en, this message translates to:
  /// **'Cancel'**
  String get cancel;

  /// No description provided for @delete.
  ///
  /// In en, this message translates to:
  /// **'Delete'**
  String get delete;

  /// No description provided for @settingsWelcomeTitle.
  ///
  /// In en, this message translates to:
  /// **'Welcome — one step to start logging'**
  String get settingsWelcomeTitle;

  /// No description provided for @settingsWelcomeBody.
  ///
  /// In en, this message translates to:
  /// **'Tap AI Provider below, pick a provider and paste its API key (it saves as you type), then Test This Provider.\nThen turn on Watch Camera Roll and new food photos log themselves.\nIn mainland China choose Qwen 通义千问, Doubao 豆包 or GLM 智谱 (GLM\'s default model is free) — the other providers need a VPN. 中国大陆用户请选择国内提供商。'**
  String get settingsWelcomeBody;

  /// No description provided for @settingsSectionAi.
  ///
  /// In en, this message translates to:
  /// **'AI'**
  String get settingsSectionAi;

  /// No description provided for @settingsAiFooterPaused.
  ///
  /// In en, this message translates to:
  /// **'Analyses are paused — the daily quota was hit. New photos are kept and retried automatically; changing the key or provider on the AI Provider page resumes now.'**
  String get settingsAiFooterPaused;

  /// No description provided for @settingsAiFooter.
  ///
  /// In en, this message translates to:
  /// **'Photos are analysed by the provider you pick — its key never leaves this phone.'**
  String get settingsAiFooter;

  /// No description provided for @settingsRowAiProvider.
  ///
  /// In en, this message translates to:
  /// **'AI Provider'**
  String get settingsRowAiProvider;

  /// No description provided for @settingsSectionPhotos.
  ///
  /// In en, this message translates to:
  /// **'Photos'**
  String get settingsSectionPhotos;

  /// No description provided for @settingsPhotosFooter.
  ///
  /// In en, this message translates to:
  /// **'The watcher logs new food photos automatically; the lookback window decides how far back catch-up scans reach.'**
  String get settingsPhotosFooter;

  /// No description provided for @settingsRowWatch.
  ///
  /// In en, this message translates to:
  /// **'Watch Camera Roll'**
  String get settingsRowWatch;

  /// No description provided for @settingsRowLookback.
  ///
  /// In en, this message translates to:
  /// **'Backfill Lookback'**
  String get settingsRowLookback;

  /// No description provided for @lookbackDays.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{1 day} other{{count} days}}'**
  String lookbackDays(int count);

  /// No description provided for @settingsRowCoverage.
  ///
  /// In en, this message translates to:
  /// **'Photo Coverage'**
  String get settingsRowCoverage;

  /// No description provided for @settingsSectionReport.
  ///
  /// In en, this message translates to:
  /// **'Report'**
  String get settingsSectionReport;

  /// No description provided for @settingsRowReportTime.
  ///
  /// In en, this message translates to:
  /// **'Report Time'**
  String get settingsRowReportTime;

  /// No description provided for @settingsSectionProfile.
  ///
  /// In en, this message translates to:
  /// **'Profile'**
  String get settingsSectionProfile;

  /// No description provided for @settingsRowDietaryProfile.
  ///
  /// In en, this message translates to:
  /// **'Dietary Profile'**
  String get settingsRowDietaryProfile;

  /// No description provided for @profileSet.
  ///
  /// In en, this message translates to:
  /// **'Set'**
  String get profileSet;

  /// No description provided for @profileNotSet.
  ///
  /// In en, this message translates to:
  /// **'Not set'**
  String get profileNotSet;

  /// No description provided for @settingsSectionData.
  ///
  /// In en, this message translates to:
  /// **'Your Data'**
  String get settingsSectionData;

  /// No description provided for @settingsDataFooter.
  ///
  /// In en, this message translates to:
  /// **'Import MERGES an exported file into this phone: meals already here are left alone, so importing twice never doubles your calories.'**
  String get settingsDataFooter;

  /// No description provided for @settingsRowExport.
  ///
  /// In en, this message translates to:
  /// **'Export Data…'**
  String get settingsRowExport;

  /// No description provided for @settingsRowImport.
  ///
  /// In en, this message translates to:
  /// **'Import Data…'**
  String get settingsRowImport;

  /// No description provided for @settingsRowLanguage.
  ///
  /// In en, this message translates to:
  /// **'Language'**
  String get settingsRowLanguage;

  /// No description provided for @languageSystem.
  ///
  /// In en, this message translates to:
  /// **'System'**
  String get languageSystem;

  /// No description provided for @languageSheetTitle.
  ///
  /// In en, this message translates to:
  /// **'Language'**
  String get languageSheetTitle;

  /// No description provided for @lookbackSheetTitle.
  ///
  /// In en, this message translates to:
  /// **'Backfill lookback'**
  String get lookbackSheetTitle;

  /// No description provided for @lookbackSheetHint.
  ///
  /// In en, this message translates to:
  /// **'How many days catch-up scans look back.'**
  String get lookbackSheetHint;

  /// No description provided for @lookbackSet.
  ///
  /// In en, this message translates to:
  /// **'Set {count, plural, =1{1 day} other{{count} days}}'**
  String lookbackSet(int count);

  /// No description provided for @providerPageTitle.
  ///
  /// In en, this message translates to:
  /// **'AI Provider'**
  String get providerPageTitle;

  /// No description provided for @connectionTypeHeader.
  ///
  /// In en, this message translates to:
  /// **'Connection type'**
  String get connectionTypeHeader;

  /// No description provided for @connectionTypeFooter.
  ///
  /// In en, this message translates to:
  /// **'An API key pays per photo and lives on this phone. A subscription is a flat-rate plan your own cloud server signs into — photos cost nothing extra.'**
  String get connectionTypeFooter;

  /// No description provided for @typeApiKey.
  ///
  /// In en, this message translates to:
  /// **'API Key'**
  String get typeApiKey;

  /// No description provided for @typeSubscription.
  ///
  /// In en, this message translates to:
  /// **'Subscription'**
  String get typeSubscription;

  /// No description provided for @testProvider.
  ///
  /// In en, this message translates to:
  /// **'Test This Provider'**
  String get testProvider;

  /// No description provided for @testProviderFooter.
  ///
  /// In en, this message translates to:
  /// **'The test names exactly what is broken: configuration, network, key, account credit, reply format, or quota.'**
  String get testProviderFooter;

  /// No description provided for @apiPageTitle.
  ///
  /// In en, this message translates to:
  /// **'API Key'**
  String get apiPageTitle;

  /// No description provided for @apiProviderHeader.
  ///
  /// In en, this message translates to:
  /// **'Provider'**
  String get apiProviderHeader;

  /// No description provided for @apiProviderFooter.
  ///
  /// In en, this message translates to:
  /// **'Pay per photo: the key lives on this phone and every photo is a metered API call billed by the vendor. In mainland China choose Qwen, Doubao or GLM — the others need a VPN. 中国大陆用户请选择国内提供商。'**
  String get apiProviderFooter;

  /// No description provided for @noteVpn.
  ///
  /// In en, this message translates to:
  /// **'VPN in China'**
  String get noteVpn;

  /// No description provided for @noteFreeTierVpn.
  ///
  /// In en, this message translates to:
  /// **'free tier · VPN in China'**
  String get noteFreeTierVpn;

  /// No description provided for @noteDirect.
  ///
  /// In en, this message translates to:
  /// **'direct in China'**
  String get noteDirect;

  /// No description provided for @noteFreeDirect.
  ///
  /// In en, this message translates to:
  /// **'free · direct in China'**
  String get noteFreeDirect;

  /// No description provided for @apiKeyHeader.
  ///
  /// In en, this message translates to:
  /// **'API key'**
  String get apiKeyHeader;

  /// No description provided for @apiKeyLabel.
  ///
  /// In en, this message translates to:
  /// **'{provider} API key'**
  String apiKeyLabel(String provider);

  /// No description provided for @apiKeyFooterDefault.
  ///
  /// In en, this message translates to:
  /// **'The key saves as you type. Stored securely on this device only.'**
  String get apiKeyFooterDefault;

  /// No description provided for @apiKeyFooterQwen.
  ///
  /// In en, this message translates to:
  /// **'From bailian.console.aliyun.com (Alibaba Cloud 百炼 → API-KEY). New accounts get ~1M free tokens per model. Stored securely on this device only.'**
  String get apiKeyFooterQwen;

  /// No description provided for @apiKeyFooterDoubao.
  ///
  /// In en, this message translates to:
  /// **'From console.volcengine.com/ark (API Key + 开通管理 to activate models). 500k free tokens per model. Stored securely on this device only.'**
  String get apiKeyFooterDoubao;

  /// No description provided for @apiKeyFooterGlm.
  ///
  /// In en, this message translates to:
  /// **'From open.bigmodel.cn (real-name verification required). The default flash model is free. Stored securely on this device only.'**
  String get apiKeyFooterGlm;

  /// No description provided for @modelHeader.
  ///
  /// In en, this message translates to:
  /// **'Model'**
  String get modelHeader;

  /// No description provided for @modelCustomRow.
  ///
  /// In en, this message translates to:
  /// **'Custom — type a model name…'**
  String get modelCustomRow;

  /// No description provided for @modelCustomLabel.
  ///
  /// In en, this message translates to:
  /// **'Custom model name'**
  String get modelCustomLabel;

  /// No description provided for @apiInactiveFooter.
  ///
  /// In en, this message translates to:
  /// **'A subscription is currently active. Pick a provider above to switch to a pay-per-photo API key.'**
  String get apiInactiveFooter;

  /// No description provided for @subPageTitle.
  ///
  /// In en, this message translates to:
  /// **'Subscription'**
  String get subPageTitle;

  /// No description provided for @planHeader.
  ///
  /// In en, this message translates to:
  /// **'Plan'**
  String get planHeader;

  /// No description provided for @planFooter.
  ///
  /// In en, this message translates to:
  /// **'Flat-rate: your own cloud machine signs in to ONE plan and analyses photos under it — each photo costs nothing extra. Plan credentials stay on that machine.'**
  String get planFooter;

  /// No description provided for @planClaude.
  ///
  /// In en, this message translates to:
  /// **'Claude Plan'**
  String get planClaude;

  /// No description provided for @planClaudeNote.
  ///
  /// In en, this message translates to:
  /// **'Anthropic subscription'**
  String get planClaudeNote;

  /// No description provided for @planGlm.
  ///
  /// In en, this message translates to:
  /// **'GLM Coding Plan'**
  String get planGlm;

  /// No description provided for @planDoubao.
  ///
  /// In en, this message translates to:
  /// **'Doubao Agent Plan'**
  String get planDoubao;

  /// No description provided for @serverHeader.
  ///
  /// In en, this message translates to:
  /// **'Your server'**
  String get serverHeader;

  /// No description provided for @serverFooter.
  ///
  /// In en, this message translates to:
  /// **'The cloud machine that holds your plan sign-in and runs the analysis — not this phone. This phone keeps only the upload key it uses to talk to that machine.'**
  String get serverFooter;

  /// No description provided for @serverAddressLabel.
  ///
  /// In en, this message translates to:
  /// **'Server address'**
  String get serverAddressLabel;

  /// No description provided for @serverUploadKeyLabel.
  ///
  /// In en, this message translates to:
  /// **'Server upload key'**
  String get serverUploadKeyLabel;

  /// No description provided for @connectClaude.
  ///
  /// In en, this message translates to:
  /// **'Connect Claude'**
  String get connectClaude;

  /// No description provided for @connectClaudeFooter.
  ///
  /// In en, this message translates to:
  /// **'Signs this server in to your Anthropic subscription. Needs a VPN in mainland China.'**
  String get connectClaudeFooter;

  /// No description provided for @subInactiveFooter.
  ///
  /// In en, this message translates to:
  /// **'An API key is currently active. Pick a plan above to switch to subscription analysis via your server.'**
  String get subInactiveFooter;

  /// No description provided for @connectDialogTitle.
  ///
  /// In en, this message translates to:
  /// **'Finish connecting Claude'**
  String get connectDialogTitle;

  /// No description provided for @connectDialogBody.
  ///
  /// In en, this message translates to:
  /// **'Sign in on the Anthropic page that just opened. It will show you a code — paste it here.'**
  String get connectDialogBody;

  /// No description provided for @connectCodeLabel.
  ///
  /// In en, this message translates to:
  /// **'Authorization code'**
  String get connectCodeLabel;

  /// No description provided for @connect.
  ///
  /// In en, this message translates to:
  /// **'Connect'**
  String get connect;

  /// No description provided for @connectStartFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not start the sign-in.'**
  String get connectStartFailed;

  /// No description provided for @connectBrowserFailed.
  ///
  /// In en, this message translates to:
  /// **'Could not open the browser.'**
  String get connectBrowserFailed;

  /// No description provided for @connectDone.
  ///
  /// In en, this message translates to:
  /// **'Claude connected — analyses run on your subscription.'**
  String get connectDone;

  /// No description provided for @profilePageTitle.
  ///
  /// In en, this message translates to:
  /// **'Dietary Profile'**
  String get profilePageTitle;

  /// No description provided for @profileFooter.
  ///
  /// In en, this message translates to:
  /// **'Preferences and cultural context the AI reads alongside every photo — e.g. \"vegetarian\", \"Cantonese home cooking, light oil\", \"cutting, high protein\". Saves as you type.'**
  String get profileFooter;

  /// No description provided for @profileHint.
  ///
  /// In en, this message translates to:
  /// **'Nothing yet — the AI assumes no special preferences.'**
  String get profileHint;

  /// No description provided for @addSheetTitle.
  ///
  /// In en, this message translates to:
  /// **'Log a meal'**
  String get addSheetTitle;

  /// No description provided for @addFromPhotos.
  ///
  /// In en, this message translates to:
  /// **'From recent photos'**
  String get addFromPhotos;

  /// No description provided for @addDescribe.
  ///
  /// In en, this message translates to:
  /// **'Describe a meal'**
  String get addDescribe;

  /// No description provided for @addDescribeNote.
  ///
  /// In en, this message translates to:
  /// **'any language'**
  String get addDescribeNote;

  /// No description provided for @addManual.
  ///
  /// In en, this message translates to:
  /// **'Enter manually'**
  String get addManual;

  /// No description provided for @addManualNote.
  ///
  /// In en, this message translates to:
  /// **'no AI'**
  String get addManualNote;

  /// No description provided for @addFix.
  ///
  /// In en, this message translates to:
  /// **'Fix or delete a meal'**
  String get addFix;

  /// No description provided for @addFixFooter.
  ///
  /// In en, this message translates to:
  /// **'\"meal 2 was roast duck\" · \"删除第一餐\"'**
  String get addFixFooter;

  /// No description provided for @addPhotosTip.
  ///
  /// In en, this message translates to:
  /// **'Tip: chopsticks or a hand in the shot helps the AI judge portion sizes.'**
  String get addPhotosTip;

  /// No description provided for @addNoPhotos.
  ///
  /// In en, this message translates to:
  /// **'No recent photos found.'**
  String get addNoPhotos;

  /// No description provided for @analyzing.
  ///
  /// In en, this message translates to:
  /// **'Analyzing…'**
  String get analyzing;

  /// No description provided for @reportTitle.
  ///
  /// In en, this message translates to:
  /// **'Daily intake'**
  String get reportTitle;

  /// No description provided for @reportMeals.
  ///
  /// In en, this message translates to:
  /// **'{count, plural, =1{1 meal} other{{count} meals}}'**
  String reportMeals(int count);

  /// No description provided for @reportNoMeals.
  ///
  /// In en, this message translates to:
  /// **'No meals logged.'**
  String get reportNoMeals;

  /// No description provided for @reportFooter.
  ///
  /// In en, this message translates to:
  /// **'Logged with CalorieTracker'**
  String get reportFooter;

  /// No description provided for @typicalDayHeadroom.
  ///
  /// In en, this message translates to:
  /// **'Typical day: ~{typical} kcal · ~{delta} kcal headroom'**
  String typicalDayHeadroom(String typical, String delta);

  /// No description provided for @typicalDayOver.
  ///
  /// In en, this message translates to:
  /// **'Typical day: ~{typical} kcal · ~{delta} kcal above typical'**
  String typicalDayOver(String typical, String delta);

  /// intl DateFormat PATTERN for dated history-day labels, not display text. en: "Tuesday, Jul 15"; zh: "7月15日 星期二".
  ///
  /// In en, this message translates to:
  /// **'EEEE, MMM dd'**
  String get historyDayPattern;

  /// No description provided for @garminBurnLine.
  ///
  /// In en, this message translates to:
  /// **'Active burn: ~{burn} kcal (Garmin) · net ~{net} kcal'**
  String garminBurnLine(String burn, String net);
}

class _AppLocalizationsDelegate
    extends LocalizationsDelegate<AppLocalizations> {
  const _AppLocalizationsDelegate();

  @override
  Future<AppLocalizations> load(Locale locale) {
    return SynchronousFuture<AppLocalizations>(lookupAppLocalizations(locale));
  }

  @override
  bool isSupported(Locale locale) =>
      <String>['en', 'zh'].contains(locale.languageCode);

  @override
  bool shouldReload(_AppLocalizationsDelegate old) => false;
}

AppLocalizations lookupAppLocalizations(Locale locale) {
  // Lookup logic when only language code is specified.
  switch (locale.languageCode) {
    case 'en':
      return AppLocalizationsEn();
    case 'zh':
      return AppLocalizationsZh();
  }

  throw FlutterError(
    'AppLocalizations.delegate failed to load unsupported locale "$locale". This is likely '
    'an issue with the localizations generation tool. Please file an issue '
    'on GitHub with a reproducible sample app and the gen-l10n configuration '
    'that was used.',
  );
}
