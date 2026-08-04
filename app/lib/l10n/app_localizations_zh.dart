// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for Chinese (`zh`).
class AppLocalizationsZh extends AppLocalizations {
  AppLocalizationsZh([String locale = 'zh']) : super(locale);

  @override
  String get tabToday => '今天';

  @override
  String get tabHistory => '历史';

  @override
  String get tabBody => '身体';

  @override
  String get tabSettings => '设置';

  @override
  String kcalAmount(String kcal) {
    return '$kcal 千卡';
  }

  @override
  String mealsToday(int count) {
    return '今天 $count 餐';
  }

  @override
  String get rowTypical => '日常';

  @override
  String get rowBurn => '消耗';

  @override
  String get rowEaten => '已摄入';

  @override
  String get rowResultLeft => '= 剩余';

  @override
  String get rowResultOver => '= 超出日常';

  @override
  String get ringHeadroom => '剩余';

  @override
  String get ringLeftToday => '今日剩余';

  @override
  String get ringAboveTypical => '超出日常';

  @override
  String get ringKcalToday => '千卡';

  @override
  String get todayEmptyTitle => '今天还没有记录。';

  @override
  String get todayEmptyHint =>
      '点击下方\"记录\"，用照片或文字添加一餐；也可以在设置里打开\"监控相册\"，新的食物照片会自动记录。';

  @override
  String get fabLog => '记录';

  @override
  String get shareDayTooltip => '把今天分享为图片';

  @override
  String shareDayFailed(String error) {
    return '生成图片失败：$error';
  }

  @override
  String historyAverage(String kcal) {
    return '平均：约 $kcal 千卡 / 天';
  }

  @override
  String get historyNoMeals => '无记录';

  @override
  String historyEmpty(int days) {
    return '过去 $days 天没有记录。';
  }

  @override
  String get retry => '重试';

  @override
  String get bodyWeightHeader => '体重';

  @override
  String get bodyMeasurementsHeader => '围度';

  @override
  String get bodyHistoryHeader => '历史';

  @override
  String bodyOnDate(String date) {
    return '$date';
  }

  @override
  String get bodyNoChange => '无变化';

  @override
  String get bodyWaist => '腰围';

  @override
  String get bodyChest => '胸围';

  @override
  String get bodyHip => '臀围';

  @override
  String get bodyEmptyTitle => '还没有身体数据。';

  @override
  String get bodyEmptyHint =>
      '点击\"记录\"来记体重或腰围、胸围、臀围。通过对话记录的体重（\"我今天 81.6 公斤\"）也会显示在这里。';

  @override
  String bodySheetLogTitle(String date) {
    return '记录身体 · $date';
  }

  @override
  String bodySheetEditTitle(String date) {
    return '编辑 $date';
  }

  @override
  String get bodySheetHint => '没量的项目留空即可。';

  @override
  String get bodyFieldWeight => '体重';

  @override
  String get save => '保存';

  @override
  String get saving => '保存中…';

  @override
  String bodyErrNotNumber(String label, String raw) {
    return '$label：\"$raw\" 不是数字。';
  }

  @override
  String bodyErrBounds(String label, String min, String max) {
    return '$label需要在 $min 到 $max 之间。';
  }

  @override
  String get bodyErrEmpty => '请至少填写一项。';

  @override
  String bodyDeleteTitle(String date) {
    return '删除 $date？';
  }

  @override
  String bodyDeleteBody(String what) {
    return '将删除这一天记录的$what。';
  }

  @override
  String get bodyDeleteWeight => '体重';

  @override
  String get bodyDeleteMeasurements => '围度';

  @override
  String get bodyDeleteBoth => '体重和围度';

  @override
  String get cancel => '取消';

  @override
  String get delete => '删除';

  @override
  String get settingsWelcomeTitle => '欢迎 — 一步开始记录';

  @override
  String get settingsWelcomeBody =>
      '点击下方\"AI 服务\"，选择一家并粘贴它的 API Key（输入即保存），然后\"测试当前服务\"。\n之后打开\"监控相册\"，新的食物照片会自动记录。\n中国大陆用户请选择 Qwen 通义千问、Doubao 豆包或 GLM 智谱（GLM 默认模型免费）——其余服务需要 VPN。';

  @override
  String get settingsSectionAi => 'AI';

  @override
  String get settingsAiFooterPaused =>
      '分析已暂停 — 已达当日额度。新照片会保留并自动重试；在 AI 服务页更换 Key 或服务后立即恢复。';

  @override
  String get settingsAiFooter => '照片由你选择的服务分析 — 它的 Key 不会离开这台手机。';

  @override
  String get settingsRowAiProvider => 'AI 服务';

  @override
  String get settingsSectionPhotos => '照片';

  @override
  String get settingsPhotosFooter => '监控会自动记录新的食物照片；回溯窗口决定补扫时往回看多少天。';

  @override
  String get settingsRowWatch => '监控相册';

  @override
  String get settingsRowLookback => '补扫回溯';

  @override
  String lookbackDays(int count) {
    return '$count 天';
  }

  @override
  String get settingsRowCoverage => '照片覆盖检查';

  @override
  String get settingsSectionReport => '报告';

  @override
  String get settingsRowReportTime => '报告时间';

  @override
  String get settingsSectionProfile => '个人资料';

  @override
  String get settingsRowDietaryProfile => '饮食偏好';

  @override
  String get profileSet => '已设置';

  @override
  String get profileNotSet => '未设置';

  @override
  String get settingsSectionData => '你的数据';

  @override
  String get settingsDataFooter => '导入会把导出的文件合并进这台手机：已有的餐不会被改动，导入两次也不会让热量翻倍。';

  @override
  String get settingsRowExport => '导出数据…';

  @override
  String get settingsRowImport => '导入数据…';

  @override
  String get settingsRowLanguage => '语言';

  @override
  String get languageSystem => '跟随系统';

  @override
  String get languageSheetTitle => '语言';

  @override
  String get lookbackSheetTitle => '补扫回溯';

  @override
  String get lookbackSheetHint => '补扫时往回检查多少天的照片。';

  @override
  String lookbackSet(int count) {
    return '设为 $count 天';
  }

  @override
  String get providerPageTitle => 'AI 服务';

  @override
  String get connectionTypeHeader => '连接方式';

  @override
  String get connectionTypeFooter =>
      'API Key 按张照片计费，Key 保存在这台手机上。订阅是固定月费套餐，由你自己的云服务器登录使用 — 照片不再额外花钱。';

  @override
  String get typeApiKey => 'API Key';

  @override
  String get typeSubscription => '订阅';

  @override
  String get testProvider => '测试当前服务';

  @override
  String get testProviderFooter => '测试会指出具体问题：配置、网络、Key、账户余额、返回格式或额度。';

  @override
  String get apiPageTitle => 'API Key';

  @override
  String get apiProviderHeader => '服务商';

  @override
  String get apiProviderFooter =>
      '按张计费：Key 保存在这台手机上，每张照片都是一次由服务商计费的 API 调用。中国大陆请选择 Qwen、Doubao 或 GLM — 其余需要 VPN。';

  @override
  String get noteVpn => '需要 VPN';

  @override
  String get noteFreeTierVpn => '有免费额度 · 需要 VPN';

  @override
  String get noteDirect => '中国直连';

  @override
  String get noteFreeDirect => '免费 · 中国直连';

  @override
  String get apiKeyHeader => 'API Key';

  @override
  String apiKeyLabel(String provider) {
    return '$provider API Key';
  }

  @override
  String get apiKeyFooterDefault => '输入即保存。仅安全存储在本机。';

  @override
  String get apiKeyFooterQwen =>
      '从 bailian.console.aliyun.com（阿里云百炼 → API-KEY）获取。新账户每个模型约有 100 万免费 tokens。仅安全存储在本机。';

  @override
  String get apiKeyFooterDoubao =>
      '从 console.volcengine.com/ark 获取（API Key + 开通管理里激活模型）。每个模型 50 万免费 tokens。仅安全存储在本机。';

  @override
  String get apiKeyFooterGlm =>
      '从 open.bigmodel.cn 获取（需实名认证）。默认 flash 模型免费。仅安全存储在本机。';

  @override
  String get modelHeader => '模型';

  @override
  String get modelCustomRow => '自定义 — 输入模型名…';

  @override
  String get modelCustomLabel => '自定义模型名';

  @override
  String get apiInactiveFooter => '当前使用的是订阅。在上方选择一家服务商即可切换为按张计费的 API Key。';

  @override
  String get subPageTitle => '订阅';

  @override
  String get planHeader => '套餐';

  @override
  String get planFooter =>
      '固定月费：你自己的云服务器登录一个套餐并用它分析照片 — 每张照片不再额外花钱。套餐凭证保存在那台机器上。';

  @override
  String get planClaude => 'Claude 订阅';

  @override
  String get planClaudeNote => 'Anthropic 订阅';

  @override
  String get planGlm => 'GLM 编程套餐';

  @override
  String get planDoubao => '豆包 Agent 套餐';

  @override
  String get serverHeader => '你的服务器';

  @override
  String get serverFooter => '保存套餐登录并执行分析的云端机器 — 不是这台手机。手机上只保存与它通信的上传密钥。';

  @override
  String get serverAddressLabel => '服务器地址';

  @override
  String get serverUploadKeyLabel => '服务器上传密钥';

  @override
  String get connectClaude => '连接 Claude';

  @override
  String get connectClaudeFooter => '让服务器登录你的 Anthropic 订阅。中国大陆需要 VPN。';

  @override
  String get subInactiveFooter => '当前使用的是 API Key。在上方选择一个套餐即可切换为经你服务器的订阅分析。';

  @override
  String get connectDialogTitle => '完成 Claude 连接';

  @override
  String get connectDialogBody => '在刚打开的 Anthropic 页面登录，它会显示一个代码 — 粘贴到这里。';

  @override
  String get connectCodeLabel => '授权代码';

  @override
  String get connect => '连接';

  @override
  String get connectStartFailed => '无法开始登录。';

  @override
  String get connectBrowserFailed => '无法打开浏览器。';

  @override
  String get connectDone => 'Claude 已连接 — 分析将使用你的订阅。';

  @override
  String get profilePageTitle => '饮食偏好';

  @override
  String get profileFooter =>
      'AI 分析每张照片时都会参考的偏好和背景 — 例如\"素食\"、\"广式家常菜，少油\"、\"减脂期，高蛋白\"。输入即保存。';

  @override
  String get profileHint => '还没有内容 — AI 将按无特殊偏好处理。';

  @override
  String get addSheetTitle => '记录一餐';

  @override
  String get addFromPhotos => '从最近照片选择';

  @override
  String get addDescribe => '文字描述一餐';

  @override
  String get addDescribeNote => '任何语言';

  @override
  String get addManual => '手动输入';

  @override
  String get addManualNote => '不用 AI';

  @override
  String get addFix => '修改或删除某餐';

  @override
  String get addFixFooter => '\"第二餐是烤鸭\" · \"删除第一餐\"';

  @override
  String get addPhotosTip => '小提示：照片里带上筷子或手，AI 估算分量更准。';

  @override
  String get addNoPhotos => '没有找到最近的照片。';

  @override
  String get analyzing => '分析中…';

  @override
  String get reportTitle => '今日饮食';

  @override
  String reportMeals(int count) {
    return '$count 餐';
  }

  @override
  String get reportNoMeals => '没有记录。';

  @override
  String get reportFooter => '由 CalorieTracker 记录';

  @override
  String typicalDayHeadroom(String typical, String delta) {
    return '日常：~$typical 千卡 · 剩余 ~$delta 千卡';
  }

  @override
  String typicalDayOver(String typical, String delta) {
    return '日常：~$typical 千卡 · 超出 ~$delta 千卡';
  }

  @override
  String get historyDayPattern => 'M月d日 EEEE';

  @override
  String garminBurnLine(String burn, String net) {
    return '活动消耗：~$burn 千卡（Garmin）· 净摄入 ~$net 千卡';
  }

  @override
  String get settingsRowUnits => '单位';

  @override
  String get unitsMetric => '公制';

  @override
  String get unitsImperial => '英制';

  @override
  String get unitsMetricDetail => '公制 — 公斤 · 厘米';

  @override
  String get unitsImperialDetail => '英制 — 磅 · 英寸';

  @override
  String get unitsSheetTitle => '单位';

  @override
  String get unitsFooter => '身体体重与围度的显示和输入单位。食物始终使用克与千卡。';

  @override
  String get bodyEmptyHintImperial =>
      '点击\"记录\"来记体重或腰围、胸围、臀围。通过对话记录的体重（\"我今天 81.6 公斤\"）也会显示在这里。';

  @override
  String bodySince(String date) {
    return '自 $date';
  }

  @override
  String get addLeftover => '记录剩菜';

  @override
  String get addLeftoverNote => '扣除没吃完的部分';

  @override
  String get leftoverTitle => '剩菜扣除';

  @override
  String get leftoverPickMeal => '这是哪一餐的剩菜？';

  @override
  String get leftoverPickPhoto => '选择剩下食物的照片 — 这餐的热量会减为实际吃掉的部分。';

  @override
  String get leftoverChangeMeal => '更换';

  @override
  String get leftoverNotSame => '照片看起来不是这一餐';

  @override
  String get leftoverUseAnyway => '仍然使用';

  @override
  String get leftoverResultTitle => '确认扣除剩菜？';

  @override
  String leftoverResultLine(String pct, String kcal, String now) {
    return '吃了约 $pct% — 扣除 $kcal 千卡，这餐现在 $now 千卡。';
  }

  @override
  String leftoverDupRemoved(String kcal) {
    return '这张照片还被误记成了一餐（$kcal 千卡）— 该重复记录将一并删除。';
  }

  @override
  String get leftoverApplied => '已扣除剩菜。';

  @override
  String get leftoverFailed => '无法从这张照片估算剩菜。';

  @override
  String get leftoverNoMeals => '今天和昨天没有可扣除的餐。';
}
