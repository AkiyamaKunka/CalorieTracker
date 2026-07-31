/// Onboarding/Settings (spec §8 knobs): Gemini key with live validateKey
/// feedback, model, backfill-lookback slider, report time, watcher toggle
/// (requests photo permission), dietary profile, JSON export via share_plus.
library;

import 'dart:async' show unawaited;

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';
import 'package:url_launcher/url_launcher.dart' as launcher;

import '../../core/contracts.dart';
import 'package:flutter/foundation.dart' show compute;

import '../../services/analyzer/normalize.dart' show makeMealThumb;
import '../../services/photo/coverage.dart';
import '../../services/photo/photo_library.dart';
import '../diagnostics.dart';
import '../photo_pipeline.dart';
import '../format.dart' show isoDate;
import '../services.dart';
import 'coverage_screen.dart';
import 'diagnostics_screen.dart';
import 'meal_editor_screen.dart';

/// Sentinel value for the model picker's "type it yourself" row.
const String _kCustomModel = '__custom__';

/// Curated vision-capable models per provider (verified 2026-07 research),
/// with the tradeoff IN the label — users pick, they don't memorize vendor
/// id strings like doubao-seed-2-0-mini-260428. The Custom row reveals a
/// text field so a model a vendor ships NEXT month never bricks the app.
const Map<String, List<(String, String)>> _knownModels = {
  'gemini': [
    ('gemini-2.5-flash', 'gemini-2.5-flash — fast, free-tier default'),
    ('gemini-2.5-pro', 'gemini-2.5-pro — strongest, slower'),
  ],
  'openai': [
    ('gpt-4o-mini', 'gpt-4o-mini — cheap, default'),
    ('gpt-4o', 'gpt-4o — stronger, pricier'),
  ],
  'anthropic': [
    ('claude-sonnet-5', 'claude-sonnet-5 — default'),
    ('claude-haiku-4-5', 'claude-haiku-4-5 — fastest, cheapest'),
    ('claude-opus-5', 'claude-opus-5 — strongest, priciest'),
  ],
  'qwen': [
    ('qwen3-vl-flash', 'qwen3-vl-flash — cheapest, default'),
    ('qwen3-vl-plus', 'qwen3-vl-plus — stronger, paid'),
    ('qwen-vl-max', 'qwen-vl-max — legacy flagship'),
  ],
  'doubao': [
    ('doubao-seed-2-0-mini-260428', 'seed-2.0 mini — cheapest, default'),
    ('doubao-seed-2-0-lite-260428', 'seed-2.0 lite — mid tier'),
    ('doubao-seed-2-1-turbo-260628', 'seed-2.1 turbo — stronger'),
    ('doubao-seed-2-1-pro-260628', 'seed-2.1 pro — strongest'),
  ],
  'glm': [
    ('glm-4.6v-flash', 'glm-4.6v-flash — FREE 免费, default'),
    ('glm-4.6v', 'glm-4.6v — stronger, paid'),
    ('glm-5v-turbo', 'glm-5v-turbo — newest flagship'),
  ],
};

bool _isCuratedModel(String provider, String model) =>
    (_knownModels[provider] ?? const []).any((m) => m.$1 == model);

class SettingsScreen extends StatefulWidget {
  final SettingsStore settings;
  final AnalyzerService analyzer;
  final MealsDao dao;
  final Future<bool> Function() requestPhotoPermission;
  final PhotoIntake? photoIntake; // started/stopped on watcher toggle

  /// Claude OAuth re-connect (server provider). Nulls in tests hide the
  /// button; [openUrl] is injectable so widget tests need no url_launcher.
  final Future<({String? url, String? error})> Function()? startClaudeAuth;
  final Future<String?> Function(String code)? completeClaudeAuth;
  final Future<bool> Function(Uri url)? openUrl;

  /// Opens the OS app-settings page — the only remedy once the system
  /// stops re-showing the photo-permission dialog. Null hides the action.
  final Future<void> Function()? openSystemSettings;

  /// Coverage-audit pieces; all three null in tests → the tile is hidden.
  final CoverageAuditor? coverage;
  final Future<PhotoOutcome> Function(IntakePhoto photo)? processPhoto;
  final PhotoLibrary? photoLibrary;

  const SettingsScreen({
    super.key,
    required this.settings,
    required this.analyzer,
    required this.dao,
    required this.requestPhotoPermission,
    this.photoIntake,
    this.coverage,
    this.processPhoto,
    this.photoLibrary,
    this.startClaudeAuth,
    this.completeClaudeAuth,
    this.openUrl,
    this.openSystemSettings,
  });

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _keyController;
  late final TextEditingController _modelController;
  late final TextEditingController _profileController;
  late final TextEditingController _serverUrlController;
  final TextEditingController _importController = TextEditingController();
  bool _customModel = false;
  late int _lookbackDays;
  late String _reportTime;
  late bool _watcherEnabled;
  bool _exporting = false;
  bool _importing = false;

  @override
  void initState() {
    super.initState();
    final s = widget.settings;
    _keyController = TextEditingController(text: s.apiKey);
    _modelController = TextEditingController(
        text: s.model.isEmpty ? 'gemini-2.5-flash' : s.model);
    _customModel = !_isCuratedModel(s.provider, _modelController.text);
    _profileController = TextEditingController(text: s.dietaryProfile);
    _serverUrlController = TextEditingController(text: s.serverBaseUrl);
    _lookbackDays = s.lookbackDays.clamp(1, 30); // spec §6.4 range
    _reportTime = s.reportTime.isEmpty ? '21:00' : s.reportTime;
    _watcherEnabled = s.watcherEnabled;
  }

  @override
  void dispose() {
    _keyController.dispose();
    _modelController.dispose();
    _profileController.dispose();
    _serverUrlController.dispose();
    _importController.dispose();
    super.dispose();
  }

  Future<void> _toggleWatcher(bool enable) async {
    if (enable) {
      final granted = await widget.requestPhotoPermission();
      if (!granted) {
        if (!mounted) return;
        final open = widget.openSystemSettings;
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: const Text(
                'Photo library permission is required for automatic intake.'),
            // Once the OS stops re-prompting, the in-app request above is
            // a silent no-op — without this action the toggle is a
            // forever dead end.
            action: open == null
                ? null
                : SnackBarAction(
                    label: 'Open settings', onPressed: () => open())));
        return; // leave the switch off
      }
      if (!mounted) return;
      // The watcher arms fine without a usable key — it just never logs
      // anything. Flipping a switch that silently does nothing needs a
      // sentence, not silence.
      if (!widget.settings.canAnalyze) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text(widget.settings.isQuotaPaused
                ? 'Watching is on, but analyses are paused by the daily '
                    'quota — new photos will wait.'
                : "Watching is on, but photos won't be analyzed until a "
                    'working API key is set above.')));
      }
      await widget.photoIntake?.start();
      // Instant feedback on enable: sweep the lookback window right away
      // instead of waiting for the next change event / background run.
      // Only when analyses can actually succeed (key present, no quota
      // pause) — otherwise the sweep reads bytes for nothing.
      if (widget.settings.canAnalyze) {
        unawaited(widget.photoIntake
            ?.backfillScan()
            .then((_) {}, onError: (Object _) {}));
      }
    } else {
      await widget.photoIntake?.stop();
    }
    if (!mounted) return;
    setState(() => _watcherEnabled = enable);
    await widget.settings.update(watcherEnabled: enable);
  }

  Future<void> _pickReportTime() async {
    final parts = _reportTime.split(':');
    final initial = TimeOfDay(
      hour: int.tryParse(parts.first) ?? 21,
      minute: parts.length > 1 ? (int.tryParse(parts[1]) ?? 0) : 0,
    );
    final picked = await showTimePicker(context: context, initialTime: initial);
    if (picked == null || !mounted) return;
    final formatted = '${picked.hour.toString().padLeft(2, '0')}:'
        '${picked.minute.toString().padLeft(2, '0')}';
    setState(() => _reportTime = formatted);
    await widget.settings.update(reportTime: formatted);
  }

  /// Import via PASTE, deliberately: adding a file-picker plugin for a
  /// once-a-year action costs a platform dependency on both OSes, while
  /// every transfer route the owner actually uses (WeChat/AirDrop/email/
  /// Termux) can put text on the clipboard. The parser refuses anything
  /// that is not a CalorieTracker export, so a mis-paste is a message,
  /// never a corrupted log.
  Future<void> _import() async {
    // The controller is owned by the STATE, not the dialog closure: a
    // locally-created one gets disposed while the dialog's exit animation
    // is still rebuilding the field ("A TextEditingController was used
    // after being disposed" — caught by the new test).
    _importController.clear();
    final go = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Import exported data'),
        // Scrollable + bounded: an AlertDialog's content is laid out
        // against the available height, and an unbounded Column here
        // overflowed by ~97000 px on a tall viewport.
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Text(
                  'Paste the contents of an exported JSON file. Existing '
                  'meals are kept; only new ones are added.'),
              const SizedBox(height: 12),
              TextField(
                key: const Key('importField'),
                controller: _importController,
                maxLines: 4,
                autofocus: true,
                decoration: const InputDecoration(
                  hintText: '{"format":"calorie_tracker_export",…',
                  border: OutlineInputBorder(),
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('Cancel')),
          FilledButton(
              key: const Key('importConfirm'),
              onPressed: () => Navigator.of(ctx).pop(true),
              child: const Text('Import')),
        ],
      ),
    );
    final payload = _importController.text;
    if (go != true || !mounted) return;
    setState(() => _importing = true);
    try {
      final summary = await widget.dao.importJson(payload);
      if (!mounted) return;
      final meals = summary.added['meals'] ?? 0;
      final kept = summary.totalSkipped;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(summary.totalAdded == 0
              ? 'Nothing new to import — everything in that file is '
                  'already here.'
              : 'Imported $meals meal${meals == 1 ? '' : 's'} '
                  '(${summary.totalAdded} rows total)'
                  '${kept > 0 ? '; $kept already here' : ''}.')));
    } on FormatException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(e.message)));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('Import failed: $e')));
    } finally {
      if (mounted) setState(() => _importing = false);
    }
  }

  Future<void> _export() async {
    setState(() => _exporting = true);
    try {
      final json = await widget.dao.exportJson(); // spec §8 full export
      // A FILE, not intent text: Android delivers EXTRA_TEXT through one
      // binder transaction with a ~1 MB hard cap, so a year of meals used
      // to kill the share (often the app) with
      // TransactionTooLargeException — and the import feature makes a
      // real FILE the thing the user actually wants to keep.
      final dir = await getTemporaryDirectory();
      final stamp = isoDate(DateTime.now());
      final file = File('${dir.path}/calorietracker-$stamp.json');
      await file.writeAsString(json, flush: true);
      await SharePlus.instance.share(ShareParams(
          files: [XFile(file.path)],
          subject: 'CalorieTracker data export'));
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('Export failed: $e')));
      }
    } finally {
      if (mounted) setState(() => _exporting = false);
    }
  }

  String _providerLabel() => switch (widget.settings.provider) {
        'openai' => 'OpenAI',
        'anthropic' => 'Anthropic',
        'server' => 'Server',
        'qwen' => 'Qwen (DashScope)',
        'doubao' => 'Doubao (Volcengine)',
        'glm' => 'GLM (Zhipu)',
        _ => 'Gemini',
      };

  bool get _isServerProvider => widget.settings.provider == 'server';
  bool _authBusy = false;

  /// The phone half of the server's Claude OAuth re-connect: fetch the
  /// OFFICIAL Anthropic authorize URL from our server, open it in the
  /// system browser (RFC 8252: never an in-app login form), then collect
  /// the pasted code and hand it back. No credential touches this device.
  Future<void> _connectClaude() async {
    final start = widget.startClaudeAuth;
    final complete = widget.completeClaudeAuth;
    if (start == null || complete == null || _authBusy) return;
    setState(() => _authBusy = true);
    try {
      final started = await start();
      if (!mounted) return;
      if (started.url == null) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text(started.error ?? 'Could not start the sign-in.')));
        return;
      }
      final open = widget.openUrl ??
          (uri) => launcher.launchUrl(uri,
              mode: launcher.LaunchMode.externalApplication);
      final opened = await open(Uri.parse(started.url!));
      if (!mounted) return;
      if (!opened) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text('Could not open the browser.')));
        return;
      }
      final code = await showDialog<String>(
        context: context,
        barrierDismissible: false,
        builder: (ctx) {
          final ctrl = TextEditingController();
          return AlertDialog(
            title: const Text('Finish connecting Claude'),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                    'Sign in on the Anthropic page that just opened. It '
                    'will show you a code — paste it here.'),
                const SizedBox(height: 12),
                TextField(
                  key: const Key('claudeAuthCodeField'),
                  controller: ctrl,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: 'Authorization code',
                    border: OutlineInputBorder(),
                  ),
                ),
              ],
            ),
            actions: [
              TextButton(
                  onPressed: () => Navigator.of(ctx).pop(null),
                  child: const Text('Cancel')),
              FilledButton(
                key: const Key('claudeAuthSubmit'),
                onPressed: () => Navigator.of(ctx).pop(ctrl.text.trim()),
                child: const Text('Connect'),
              ),
            ],
          );
        },
      );
      if (!mounted) return;
      if (code == null || code.isEmpty) return; // user cancelled
      final error = await complete(code);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(error ??
              'Claude connected — analyses run on your subscription.')));
    } finally {
      if (mounted) setState(() => _authBusy = false);
    }
  }

  String _modelHelperText() => switch (widget.settings.provider) {
        'openai' => 'Default: gpt-4o-mini',
        'anthropic' => 'Default: claude-sonnet-5',
        'qwen' => 'Default: qwen3-vl-flash. Doubles as the cheap tier — '
            'qwen3-vl-plus is the stronger paid model.',
        'doubao' => 'Default: doubao-seed-2-0-mini-260428. Doubao needs the '
            'EXACT versioned ID from the Ark model list — undated names '
            'are rejected.',
        'glm' => 'Default: glm-4.6v-flash (free tier). glm-4.6v is the '
            'stronger paid model.',
        _ => 'Default: gemini-2.5-flash',
      };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    // Sections are separate builders: this screen grew to ~950 lines
    // and every feature landed in the same wall of children, which is
    // how the 2026-07-30 review found stale copy three sections away
    // from the code it described. Same widgets, same order.
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        ..._aiSection(theme),
        const Divider(height: 32),
        ..._diagnosticsSection(theme),
        const Divider(height: 32),
        ..._photoSection(theme),
        const Divider(height: 32),
        ..._reportSection(theme),
        const Divider(height: 32),
        ..._profileSection(theme),
        const Divider(height: 32),
        ..._dataSection(theme),
        const SizedBox(height: 24),
      ],
    );
  }

  /// Provider choice, key, model, and the states that explain them.
  List<Widget> _aiSection(ThemeData theme) => [
        // First-run: the shell deliberately lands a key-less install here
        // (app.dart onboarding), but the screen used to open cold on a bare
        // form — and everything a mainland user needs to know (which
        // providers work without a VPN, which are free) lived in code
        // comments. Gone once a key is saved.
        if (widget.settings.apiKey.trim().isEmpty) ...[
          Card(
            key: const Key('firstRunCard'),
            color: theme.colorScheme.secondaryContainer,
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Welcome — one step to start logging',
                      style: theme.textTheme.titleSmall),
                  const SizedBox(height: 6),
                  Text(
                    '1) Pick an AI provider below.  2) Paste that '
                    "provider's API key (it saves as you type).  "
                    '3) Tap "Test this provider" to check everything '
                    'works.\n'
                    'Then turn on "Watch camera roll" and new food photos '
                    'log themselves.\n'
                    'In mainland China choose Qwen 通义千问, Doubao 豆包 or '
                    'GLM 智谱 (GLM’s default model is free) — the '
                    'other providers need a VPN. 中国大陆用户请选择国内提供商。',
                    style: theme.textTheme.bodySmall,
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),
        ],
        // Not 'Gemini': this section configures whichever of the seven
        // providers is selected.
        Text('AI analysis', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        if (widget.settings.isQuotaPaused) ...[
          // The §3.3 latch silently gates EVERY automatic path (watcher,
          // catch-up, coverage) — without this banner the only trace was
          // a chat refusal if the user happened to type.
          Card(
            key: const Key('quotaPauseBanner'),
            color: theme.colorScheme.errorContainer,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Text(
                'Analyses are paused — the daily quota was hit'
                '${widget.settings.quotaPauseUntil != null ? ' (until '
                    '${TimeOfDay.fromDateTime(widget.settings.quotaPauseUntil!.toLocal()).format(context)})' : ''}. '
                'New photos are kept and retried automatically; changing '
                'the key or provider resumes now.',
                style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.onErrorContainer),
              ),
            ),
          ),
          const SizedBox(height: 12),
        ],
        DropdownButtonFormField<String>(
          key: const Key('providerDropdown'),
          initialValue: widget.settings.provider,
          decoration: const InputDecoration(
            labelText: 'AI provider',
            helperText: 'Key and model below belong to the selected '
                'provider. "My server" analyses run on your own server '
                'under your Claude, GLM, or Doubao subscription — '
                'no API charges.',
            // Without this, multi-line helpers silently truncate to ONE
            // ellipsized line (Flutter's default) — measured on-device
            // 2026-07-29; the provider guidance was unreadable.
            helperMaxLines: 5,
            border: OutlineInputBorder(),
          ),
          // Group headers + free-tier notes: seven bare names forced a
          // mainland user to discover by trial that the top four need a
          // VPN. selectedItemBuilder keeps the CLOSED field short so the
          // annotated items never overflow a phone-width field.
          selectedItemBuilder: (context) => const [
            Text('Google Gemini'),
            Text('OpenAI'),
            Text('Anthropic Claude'),
            Text('My server (subscription)'),
            Text('Alibaba Qwen 通义千问'),
            Text('ByteDance Doubao 豆包'),
            Text('Zhipu GLM 智谱'),
          ],
          items: const [
            DropdownMenuItem(
                value: 'gemini',
                child: Text('Google Gemini (free tier) — VPN in China '
                    '中国需VPN')),
            DropdownMenuItem(
                value: 'openai', child: Text('OpenAI — VPN in China')),
            DropdownMenuItem(
                value: 'anthropic',
                child: Text('Anthropic Claude — VPN in China')),
            DropdownMenuItem(
                value: 'server',
                child: Text('My server (subscription)')),
            DropdownMenuItem(
                value: 'qwen',
                child: Text('Alibaba Qwen 通义千问 — 中国直连')),
            DropdownMenuItem(
                value: 'doubao',
                child: Text('ByteDance Doubao 豆包 — 中国直连')),
            DropdownMenuItem(
                value: 'glm',
                child: Text('Zhipu GLM 智谱 (free 免费) — 中国直连')),
          ],
          onChanged: (v) async {
            if (v == null) return;
            await widget.settings.update(provider: v);
            if (!mounted) return;
            setState(() {
              // The key/model fields are provider-scoped: reload them from
              // the newly selected provider's stored values.
              _keyController.text = widget.settings.apiKey;
              _modelController.text = widget.settings.model;
              _customModel = !_isCuratedModel(
                  widget.settings.provider, widget.settings.model);
            });
          },
        ),
        const SizedBox(height: 12),
        if (_isServerProvider) ...[
          TextField(
            key: const Key('serverUrlField'),
            controller: _serverUrlController,
            autocorrect: false,
            keyboardType: TextInputType.url,
            decoration: const InputDecoration(
              labelText: 'Server address',
              hintText: 'http://your.server.ip',
              helperText: 'Your CalorieTracker server — it analyses photos '
                  'with the subscription selected below.',
              border: OutlineInputBorder(),
            ),
            onSubmitted: (v) =>
                widget.settings.update(serverBaseUrl: v.trim()),
            onTapOutside: (_) => widget.settings
                .update(serverBaseUrl: _serverUrlController.text.trim()),
          ),
          const SizedBox(height: 12),
          // Whose subscription the server spends. The vendor plan keys are
          // SERVER-side .env entries (GLM_PLAN_KEY / DOUBAO_PLAN_KEY) —
          // the phone only remembers which backend to ask for, so there is
          // no key field here. Test connection reports a missing key.
          SegmentedButton<String>(
            key: const Key('serverBackendSelector'),
            segments: const [
              ButtonSegment(value: 'claude', label: Text('Claude')),
              ButtonSegment(value: 'glm', label: Text('GLM')),
              ButtonSegment(value: 'doubao', label: Text('Doubao')),
            ],
            selected: {widget.settings.serverBackend},
            onSelectionChanged: (sel) async {
              await widget.settings.update(serverBackend: sel.single);
              if (mounted) setState(() {});
            },
          ),
          const SizedBox(height: 4),
          Text(
            'Which subscription the server analyses with: your Claude '
            'plan, a GLM Coding Plan (¥49/mo), or a Doubao Agent Plan '
            '(¥40/mo). The plan key lives on the server, not the phone.',
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: Theme.of(context).colorScheme.onSurfaceVariant),
          ),
          const SizedBox(height: 12),
        ],
        TextField(
          key: const Key('apiKeyField'),
          controller: _keyController,
          obscureText: true,
          autocorrect: false,
          // Persist like the model/server-URL fields: the key used to be
          // stored ONLY by a successful Validate, so paste-key → switch
          // tab (or validation failing while offline) silently configured
          // NOTHING while the field still showed the key. Validate stays
          // the health check; typing is the commit.
          onSubmitted: (v) => widget.settings.update(apiKey: v.trim()),
          onTapOutside: (_) =>
              widget.settings.update(apiKey: _keyController.text.trim()),
          decoration: InputDecoration(
            labelText: _isServerProvider
                ? 'Server upload key'
                : '${_providerLabel()} API key',
            helperText: switch (widget.settings.provider) {
              'server' => 'The X-API-Key your server expects. Stored '
                  'securely on this device only.',
              // Where to get a key, because for these three the user's
              // journey starts on a Chinese cloud console. Free quotas
              // as of 2026-07: Qwen ~1M tokens/model (90 days), Doubao
              // 500k tokens/model, GLM's flash vision models are free.
              'qwen' => 'From bailian.console.aliyun.com (Alibaba Cloud '
                  '百炼 → API-KEY). New accounts get ~1M free tokens per '
                  'model. Stored securely on this device only.',
              'doubao' => 'From console.volcengine.com/ark (API Key + '
                  '开通管理 to activate models). 500k free tokens per '
                  'model. Stored securely on this device only.',
              'glm' => 'From open.bigmodel.cn (real-name verification '
                  'required). The default flash model is free. Stored '
                  'securely on this device only.',
              _ => 'Stored securely on this device only.',
            },
            helperMaxLines: 5, // multi-line guidance must not ellipsize
            border: const OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 8),
        Align(
          alignment: Alignment.centerLeft,
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              // ONE test action. 'Validate key' was a strict SUBSET of the
              // diagnostics page (same validateKey call, stage 3 of six)
              // and its other job — persisting the key on success — became
              // redundant when the field started persisting on type. Two
              // buttons that both mean "check my setup", one of which
              // answers less, is the clutter this deletes.
              FilledButton.tonal(
                key: const Key('validateKeyButton'),
                onPressed: () => Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) => DiagnosticsScreen(
                    diagnostics: ProviderDiagnostics(
                      settings: widget.settings,
                      analyzer: widget.analyzer,
                    ),
                  ),
                )),
                child: const Text('Test this provider'),
              ),
              // Claude OAuth is meaningless when the server's payer is a
              // GLM/Doubao plan — and the sign-in it launches needs a VPN
              // for the mainland audience that picks those backends.
              if (_isServerProvider &&
                  widget.settings.serverBackend == 'claude' &&
                  widget.startClaudeAuth != null) ...[
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  key: const Key('connectClaudeButton'),
                  onPressed: _authBusy ? null : _connectClaude,
                  icon: _authBusy
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.link, size: 16),
                  label: const Text('Connect Claude'),
                ),
              ],
            ],
          ),
        ),
        const SizedBox(height: 12),
        // The server path has no model field: the VM's own
        // CLAUDE_ANALYZER_MODEL decides, and a phone-side value would be a
        // lie the user could not act on.
        if (!_isServerProvider) ...[
          // PICKED, not typed (user report 2026-07-31: "the model's name
          // shouldn't be typed by user but selected"): a dropdown of
          // verified models with the tradeoff in the label. KeyedSubtree:
          // a provider switch must rebuild the picker with the new
          // provider's items and selection.
          KeyedSubtree(
            key: ValueKey('modelPicker-${widget.settings.provider}'),
            child: DropdownButtonFormField<String>(
              key: const Key('modelPicker'),
              initialValue: _customModel
                  ? _kCustomModel
                  : widget.settings.model,
              decoration: const InputDecoration(
                labelText: 'Model',
                border: OutlineInputBorder(),
              ),
              items: [
                for (final (id, label) in _knownModels[
                        widget.settings.provider] ??
                    const <(String, String)>[])
                  DropdownMenuItem(value: id, child: Text(label)),
                const DropdownMenuItem(
                    value: _kCustomModel,
                    child: Text('Custom — type a model name…')),
              ],
              onChanged: (v) async {
                if (v == null) return;
                if (v == _kCustomModel) {
                  setState(() => _customModel = true);
                  return;
                }
                await widget.settings.update(model: v);
                if (!mounted) return;
                setState(() {
                  _customModel = false;
                  _modelController.text = v;
                });
              },
            ),
          ),
          if (_customModel) ...[
            const SizedBox(height: 8),
            TextField(
              key: const Key('modelField'),
              controller: _modelController,
              autocorrect: false,
              onSubmitted: (v) => widget.settings.update(model: v.trim()),
              // Tap-away must not lose the edit — onSubmitted only fires
              // on the keyboard action key.
              onTapOutside: (_) => widget.settings
                  .update(model: _modelController.text.trim()),
              decoration: InputDecoration(
                labelText: 'Custom model name',
                helperText: _modelHelperText(),
                helperMaxLines: 4, // the Doubao versioned-ID warning wraps
                border: const OutlineInputBorder(),
              ),
            ),
          ],
        ],
  ];

  /// One tap to "why is analysis failing".
  List<Widget> _diagnosticsSection(ThemeData theme) => [
        // "Why doesn't my AI work" in one tap: names the broken piece
        // (VPN, key, credit, format, quota) instead of a generic error.
        ListTile(
          key: const Key('diagnosticsTile'),
          contentPadding: EdgeInsets.zero,
          leading: const Icon(Icons.health_and_safety_outlined),
          title: const Text('Test AI provider'),
          subtitle: const Text('Find out exactly why analysis fails'),
          trailing: const Icon(Icons.chevron_right),
          onTap: () => Navigator.of(context).push(MaterialPageRoute(
            builder: (_) => DiagnosticsScreen(
              diagnostics: ProviderDiagnostics(
                settings: widget.settings,
                analyzer: widget.analyzer,
              ),
            ),
          )),
        ),
  ];

  /// Camera-roll watching, coverage audit, backfill window.
  List<Widget> _photoSection(ThemeData theme) => [
        Text('Photo intake', style: theme.textTheme.titleMedium),
        if (widget.coverage != null && widget.processPhoto != null)
          ListTile(
            key: const Key('coverageCheckTile'),
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.fact_check_outlined),
            title: const Text('Check photo coverage'),
            subtitle: const Text(
                'Verify every recent photo was scanned and logged'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () => Navigator.of(context).push(MaterialPageRoute(
              builder: (_) => CoverageScreen(
                auditor: widget.coverage!,
                processPhoto: widget.processPhoto!,
                requestPhotoPermission: widget.requestPhotoPermission,
                initialLookbackDays: _lookbackDays,
                // A closure, not a snapshot: the quota latch can arm while
                // the coverage screen's bulk run is mid-batch.
                canAnalyze: () => widget.settings.canAnalyze,
                library: widget.photoLibrary,
                logManually: (photo) async =>
                    await Navigator.of(context).push<bool>(MaterialPageRoute(
                      builder: (_) => MealEditorScreen(
                        dao: widget.dao,
                        fromPhoto: photo,
                        makeThumb: (b) => compute(makeMealThumb, b),
                      ),
                    )) ==
                    true,
              ),
            )),
          ),
        SwitchListTile(
          key: const Key('watcherToggle'),
          contentPadding: EdgeInsets.zero,
          title: const Text('Watch camera roll'),
          subtitle: const Text('Automatically log new food photos'),
          value: _watcherEnabled,
          onChanged: _toggleWatcher,
        ),
        ListTile(
          contentPadding: EdgeInsets.zero,
          title: Text('Backfill lookback: $_lookbackDays day(s)'),
          subtitle: Slider(
            key: const Key('lookbackSlider'),
            value: _lookbackDays.toDouble(),
            min: 1,
            max: 30, // spec §6.4 clamp 1–30
            divisions: 29,
            label: '$_lookbackDays',
            onChanged: (v) => setState(() => _lookbackDays = v.round()),
            onChangeEnd: (v) =>
                widget.settings.update(lookbackDays: v.round()),
          ),
        ),
  ];

  /// Daily-report time.
  List<Widget> _reportSection(ThemeData theme) => [
        Text('Daily report', style: theme.textTheme.titleMedium),
        ListTile(
          key: const Key('reportTimeTile'),
          contentPadding: EdgeInsets.zero,
          title: const Text('Report time'),
          trailing: Text(_reportTime, style: theme.textTheme.titleMedium),
          onTap: _pickReportTime,
        ),
  ];

  /// Dietary profile appended to the photo prompt (§1.3).
  List<Widget> _profileSection(ThemeData theme) => [
        Text('Dietary profile', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        TextField(
          key: const Key('dietaryProfileField'),
          controller: _profileController,
          maxLines: 4,
          onChanged: (v) => widget.settings.update(dietaryProfile: v),
          decoration: const InputDecoration(
            hintText: 'Preferences and cultural context appended to photo '
                'analysis (spec §1.3)',
            border: OutlineInputBorder(),
          ),
        ),
  ];

  /// Export and import — the food log is the user's.
  List<Widget> _dataSection(ThemeData theme) => [
        Text('Your data', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        FilledButton.icon(
          key: const Key('exportButton'),
          onPressed: _exporting ? null : _export,
          icon: _exporting
              ? const SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.ios_share),
          label: const Text('Export data (JSON)'),
        ),
        const SizedBox(height: 8),
        // The other half of export, missing until 2026-07-31: without it a
        // new phone (or a re-signed build, which Android treats as a
        // different app and wipes) meant starting the food log from zero.
        // MERGE semantics, never replace — see MealsDao.importJson.
        OutlinedButton.icon(
          key: const Key('importButton'),
          onPressed: _importing ? null : _import,
          icon: _importing
              ? const SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.file_download_outlined),
          label: const Text('Import data (JSON)'),
        ),
        const SizedBox(height: 4),
        Text(
          'Import MERGES an exported file into this phone: meals already '
          'here are left alone, so importing twice never doubles your '
          'calories. Paste the file contents in the next step.',
          style: theme.textTheme.bodySmall
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
        ),
  ];
}
