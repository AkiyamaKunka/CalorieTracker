// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for English (`en`).
class AppLocalizationsEn extends AppLocalizations {
  AppLocalizationsEn([String locale = 'en']) : super(locale);

  @override
  String get tabToday => 'Today';

  @override
  String get tabHistory => 'History';

  @override
  String get tabBody => 'Body';

  @override
  String get tabSettings => 'Settings';

  @override
  String kcalAmount(String kcal) {
    return '$kcal kcal';
  }

  @override
  String mealsToday(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count meals today',
      one: '1 meal today',
    );
    return '$_temp0';
  }

  @override
  String get rowTypical => 'Typical';

  @override
  String get rowBurn => 'Burn';

  @override
  String get rowEaten => 'Eaten';

  @override
  String get rowResultLeft => '= Left';

  @override
  String get rowResultOver => '= Above typical';

  @override
  String get ringHeadroom => 'headroom';

  @override
  String get ringLeftToday => 'left today';

  @override
  String get ringAboveTypical => 'above typical';

  @override
  String get ringKcalToday => 'kcal today';

  @override
  String get todayEmptyTitle => 'No meals logged yet today.';

  @override
  String get todayEmptyHint =>
      'Tap \"Log\" below to add one from a photo or a description — or turn on \"Watch camera roll\" in Settings and new food photos log themselves.';

  @override
  String get fabLog => 'Log';

  @override
  String get shareDayTooltip => 'Share today as an image';

  @override
  String shareDayFailed(String error) {
    return 'Could not build the image: $error';
  }

  @override
  String historyAverage(String kcal) {
    return 'Average: ~$kcal kcal / day';
  }

  @override
  String get historyNoMeals => 'no meals logged';

  @override
  String historyEmpty(int days) {
    return 'No meals logged in the past $days days.';
  }

  @override
  String get retry => 'Retry';

  @override
  String get bodyWeightHeader => 'Weight';

  @override
  String get bodyMeasurementsHeader => 'Measurements';

  @override
  String get bodyHistoryHeader => 'History';

  @override
  String bodyOnDate(String date) {
    return 'on $date';
  }

  @override
  String get bodyNoChange => 'no change';

  @override
  String get bodyWaist => 'Waist';

  @override
  String get bodyChest => 'Chest';

  @override
  String get bodyHip => 'Hip';

  @override
  String get bodyEmptyTitle => 'No body data yet.';

  @override
  String get bodyEmptyHint =>
      'Tap Log to record your weight or your waist, chest and hip measurements. Weight logged by chat (\"I weigh 81.6 kg\") lands here too.';

  @override
  String bodySheetLogTitle(String date) {
    return 'Log body · $date';
  }

  @override
  String bodySheetEditTitle(String date) {
    return 'Edit $date';
  }

  @override
  String get bodySheetHint => 'Leave anything you did not measure empty.';

  @override
  String get bodyFieldWeight => 'Weight';

  @override
  String get save => 'Save';

  @override
  String get saving => 'Saving…';

  @override
  String bodyErrNotNumber(String label, String raw) {
    return '$label: \"$raw\" is not a number.';
  }

  @override
  String bodyErrBounds(String label, String min, String max) {
    return '$label must be between $min and $max.';
  }

  @override
  String get bodyErrEmpty => 'Enter at least one value.';

  @override
  String bodyDeleteTitle(String date) {
    return 'Delete $date?';
  }

  @override
  String bodyDeleteBody(String what) {
    return 'Removes the $what recorded for this day.';
  }

  @override
  String get bodyDeleteWeight => 'weight';

  @override
  String get bodyDeleteMeasurements => 'measurements';

  @override
  String get bodyDeleteBoth => 'weight and measurements';

  @override
  String get cancel => 'Cancel';

  @override
  String get delete => 'Delete';

  @override
  String get settingsWelcomeTitle => 'Welcome — one step to start logging';

  @override
  String get settingsWelcomeBody =>
      'Tap AI Provider below, pick a provider and paste its API key (it saves as you type), then Test This Provider.\nThen turn on Watch Camera Roll and new food photos log themselves.\nIn mainland China choose Qwen 通义千问, Doubao 豆包 or GLM 智谱 (GLM\'s default model is free) — the other providers need a VPN. 中国大陆用户请选择国内提供商。';

  @override
  String get settingsSectionAi => 'AI';

  @override
  String get settingsAiFooterPaused =>
      'Analyses are paused — the daily quota was hit. New photos are kept and retried automatically; changing the key or provider on the AI Provider page resumes now.';

  @override
  String get settingsAiFooter =>
      'Photos are analysed by the provider you pick — its key never leaves this phone.';

  @override
  String get settingsRowAiProvider => 'AI Provider';

  @override
  String get settingsSectionPhotos => 'Photos';

  @override
  String get settingsPhotosFooter =>
      'The watcher logs new food photos automatically; the lookback window decides how far back catch-up scans reach.';

  @override
  String get settingsRowWatch => 'Watch Camera Roll';

  @override
  String get settingsRowLookback => 'Backfill Lookback';

  @override
  String lookbackDays(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count days',
      one: '1 day',
    );
    return '$_temp0';
  }

  @override
  String get settingsRowCoverage => 'Photo Coverage';

  @override
  String get settingsSectionReport => 'Report';

  @override
  String get settingsRowReportTime => 'Report Time';

  @override
  String get settingsSectionProfile => 'Profile';

  @override
  String get settingsRowDietaryProfile => 'Dietary Profile';

  @override
  String get profileSet => 'Set';

  @override
  String get profileNotSet => 'Not set';

  @override
  String get settingsSectionData => 'Your Data';

  @override
  String get settingsDataFooter =>
      'Import MERGES an exported file into this phone: meals already here are left alone, so importing twice never doubles your calories.';

  @override
  String get settingsRowExport => 'Export Data…';

  @override
  String get settingsRowImport => 'Import Data…';

  @override
  String get settingsRowLanguage => 'Language';

  @override
  String get languageSystem => 'System';

  @override
  String get languageSheetTitle => 'Language';

  @override
  String get lookbackSheetTitle => 'Backfill lookback';

  @override
  String get lookbackSheetHint => 'How many days catch-up scans look back.';

  @override
  String lookbackSet(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count days',
      one: '1 day',
    );
    return 'Set $_temp0';
  }

  @override
  String get providerPageTitle => 'AI Provider';

  @override
  String get connectionTypeHeader => 'Connection type';

  @override
  String get connectionTypeFooter =>
      'An API key pays per photo and lives on this phone. A subscription is a flat-rate plan your own cloud server signs into — photos cost nothing extra.';

  @override
  String get typeApiKey => 'API Key';

  @override
  String get typeSubscription => 'Subscription';

  @override
  String get testProvider => 'Test This Provider';

  @override
  String get testProviderFooter =>
      'The test names exactly what is broken: configuration, network, key, account credit, reply format, or quota.';

  @override
  String get apiPageTitle => 'API Key';

  @override
  String get apiProviderHeader => 'Provider';

  @override
  String get apiProviderFooter =>
      'Pay per photo: the key lives on this phone and every photo is a metered API call billed by the vendor. In mainland China choose Qwen, Doubao or GLM — the others need a VPN. 中国大陆用户请选择国内提供商。';

  @override
  String get noteVpn => 'VPN in China';

  @override
  String get noteFreeTierVpn => 'free tier · VPN in China';

  @override
  String get noteDirect => 'direct in China';

  @override
  String get noteFreeDirect => 'free · direct in China';

  @override
  String get apiKeyHeader => 'API key';

  @override
  String apiKeyLabel(String provider) {
    return '$provider API key';
  }

  @override
  String get apiKeyFooterDefault =>
      'The key saves as you type. Stored securely on this device only.';

  @override
  String get apiKeyFooterQwen =>
      'From bailian.console.aliyun.com (Alibaba Cloud 百炼 → API-KEY). New accounts get ~1M free tokens per model. Stored securely on this device only.';

  @override
  String get apiKeyFooterDoubao =>
      'From console.volcengine.com/ark (API Key + 开通管理 to activate models). 500k free tokens per model. Stored securely on this device only.';

  @override
  String get apiKeyFooterGlm =>
      'From open.bigmodel.cn (real-name verification required). The default flash model is free. Stored securely on this device only.';

  @override
  String get modelHeader => 'Model';

  @override
  String get modelCustomRow => 'Custom — type a model name…';

  @override
  String get modelCustomLabel => 'Custom model name';

  @override
  String get apiInactiveFooter =>
      'A subscription is currently active. Pick a provider above to switch to a pay-per-photo API key.';

  @override
  String get subPageTitle => 'Subscription';

  @override
  String get planHeader => 'Plan';

  @override
  String get planFooter =>
      'Flat-rate: your own cloud machine signs in to ONE plan and analyses photos under it — each photo costs nothing extra. Plan credentials stay on that machine.';

  @override
  String get planClaude => 'Claude Plan';

  @override
  String get planClaudeNote => 'Anthropic subscription';

  @override
  String get planGlm => 'GLM Coding Plan';

  @override
  String get planDoubao => 'Doubao Agent Plan';

  @override
  String get serverHeader => 'Your server';

  @override
  String get serverFooter =>
      'The cloud machine that holds your plan sign-in and runs the analysis — not this phone. This phone keeps only the upload key it uses to talk to that machine.';

  @override
  String get serverAddressLabel => 'Server address';

  @override
  String get serverUploadKeyLabel => 'Server upload key';

  @override
  String get connectClaude => 'Connect Claude';

  @override
  String get connectClaudeFooter =>
      'Signs this server in to your Anthropic subscription. Needs a VPN in mainland China.';

  @override
  String get subInactiveFooter =>
      'An API key is currently active. Pick a plan above to switch to subscription analysis via your server.';

  @override
  String get connectDialogTitle => 'Finish connecting Claude';

  @override
  String get connectDialogBody =>
      'Sign in on the Anthropic page that just opened. It will show you a code — paste it here.';

  @override
  String get connectCodeLabel => 'Authorization code';

  @override
  String get connect => 'Connect';

  @override
  String get connectStartFailed => 'Could not start the sign-in.';

  @override
  String get connectBrowserFailed => 'Could not open the browser.';

  @override
  String get connectDone =>
      'Claude connected — analyses run on your subscription.';

  @override
  String get profilePageTitle => 'Dietary Profile';

  @override
  String get profileFooter =>
      'Preferences and cultural context the AI reads alongside every photo — e.g. \"vegetarian\", \"Cantonese home cooking, light oil\", \"cutting, high protein\". Saves as you type.';

  @override
  String get profileHint =>
      'Nothing yet — the AI assumes no special preferences.';

  @override
  String get addSheetTitle => 'Log a meal';

  @override
  String get addFromPhotos => 'From recent photos';

  @override
  String get addDescribe => 'Describe a meal';

  @override
  String get addDescribeNote => 'any language';

  @override
  String get addManual => 'Enter manually';

  @override
  String get addManualNote => 'no AI';

  @override
  String get addFix => 'Fix or delete a meal';

  @override
  String get addFixFooter => '\"meal 2 was roast duck\" · \"删除第一餐\"';

  @override
  String get addPhotosTip =>
      'Tip: chopsticks or a hand in the shot helps the AI judge portion sizes.';

  @override
  String get addNoPhotos => 'No recent photos found.';

  @override
  String get analyzing => 'Analyzing…';

  @override
  String get reportTitle => 'Daily intake';

  @override
  String reportMeals(int count) {
    String _temp0 = intl.Intl.pluralLogic(
      count,
      locale: localeName,
      other: '$count meals',
      one: '1 meal',
    );
    return '$_temp0';
  }

  @override
  String get reportNoMeals => 'No meals logged.';

  @override
  String get reportFooter => 'Logged with CalorieTracker';

  @override
  String typicalDayHeadroom(String typical, String delta) {
    return 'Typical day: ~$typical kcal · ~$delta kcal headroom';
  }

  @override
  String typicalDayOver(String typical, String delta) {
    return 'Typical day: ~$typical kcal · ~$delta kcal above typical';
  }

  @override
  String get historyDayPattern => 'EEEE, MMM dd';

  @override
  String garminBurnLine(String burn, String net) {
    return 'Active burn: ~$burn kcal (Garmin) · net ~$net kcal';
  }

  @override
  String get settingsRowUnits => 'Units';

  @override
  String get unitsMetric => 'Metric';

  @override
  String get unitsImperial => 'Imperial';

  @override
  String get unitsMetricDetail => 'Metric — kg · cm';

  @override
  String get unitsImperialDetail => 'Imperial — lb · in';

  @override
  String get unitsSheetTitle => 'Units';

  @override
  String get unitsFooter =>
      'How body weight and measurements are shown and entered. Food stays in grams and kcal either way.';

  @override
  String get bodyEmptyHintImperial =>
      'Tap Log to record your weight or your waist, chest and hip measurements. Weight logged by chat (\"I weigh 180 lb\") lands here too.';

  @override
  String bodySince(String date) {
    return 'since $date';
  }

  @override
  String get addLeftover => 'Log leftovers';

  @override
  String get addLeftoverNote => 'deduct what you didn\'t finish';

  @override
  String get leftoverTitle => 'Leftovers';

  @override
  String get leftoverPickMeal => 'Which meal was this?';

  @override
  String get leftoverPickPhoto =>
      'Pick the photo of what\'s LEFT — the meal\'s calories shrink to what you ate.';

  @override
  String get leftoverChangeMeal => 'Change';

  @override
  String get leftoverNotSame => 'This doesn\'t look like the same meal';

  @override
  String get leftoverUseAnyway => 'Use anyway';

  @override
  String get leftoverResultTitle => 'Deduct leftovers?';

  @override
  String leftoverResultLine(String pct, String kcal, String now) {
    return 'Eaten ~$pct% — deducting $kcal kcal, this meal is now $now kcal.';
  }

  @override
  String leftoverDupRemoved(String kcal) {
    return 'This photo was also logged as its own $kcal kcal meal — that duplicate will be removed.';
  }

  @override
  String get leftoverApplied => 'Leftovers deducted.';

  @override
  String get leftoverFailed =>
      'Couldn\'t estimate the leftovers from that photo.';

  @override
  String get leftoverNoMeals =>
      'No meals from today or yesterday to deduct from.';

  @override
  String get planTuningHeader => 'Model & thinking';

  @override
  String get planTuningFooter =>
      'Which Claude model analyzes on your server, and how hard it thinks. Default follows the server\'s own setting; only the Claude plan offers this — the other plans choose models themselves.';

  @override
  String get planModelRow => 'Model';

  @override
  String get planEffortRow => 'Thinking effort';

  @override
  String get planChoiceDefault => 'Server default';

  @override
  String get planModelOpus => 'Opus — most accurate';

  @override
  String get planModelSonnet => 'Sonnet — balanced';

  @override
  String get planModelHaiku => 'Haiku — fastest';

  @override
  String get planEffortLow => 'Low — fastest';

  @override
  String get planEffortMedium => 'Medium';

  @override
  String get planEffortHigh => 'High — most thorough';
}
