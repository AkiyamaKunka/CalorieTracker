/// Onboarding/Settings (spec §8 knobs): Gemini key with live validateKey
/// feedback, model, backfill-lookback slider, report time, watcher toggle
/// (requests photo permission), dietary profile, JSON export via share_plus.
library;

import 'dart:async' show unawaited;

import 'package:flutter/material.dart';
import 'package:share_plus/share_plus.dart';
import 'package:url_launcher/url_launcher.dart' as launcher;

import '../../core/contracts.dart';
import 'package:flutter/foundation.dart' show compute;

import '../../services/analyzer/normalize.dart' show makeMealThumb;
import '../../services/photo/coverage.dart';
import '../../services/photo/photo_library.dart';
import '../photo_pipeline.dart';
import '../services.dart';
import 'coverage_screen.dart';
import 'meal_editor_screen.dart';

enum KeyValidationState { idle, validating, valid, invalid }

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
  });

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _keyController;
  late final TextEditingController _modelController;
  late final TextEditingController _profileController;
  late final TextEditingController _serverUrlController;
  KeyValidationState _keyState = KeyValidationState.idle;
  String? _keyError;
  late int _lookbackDays;
  late String _reportTime;
  late bool _watcherEnabled;
  bool _exporting = false;

  @override
  void initState() {
    super.initState();
    final s = widget.settings;
    _keyController = TextEditingController(text: s.apiKey);
    _modelController = TextEditingController(
        text: s.model.isEmpty ? 'gemini-2.5-flash' : s.model);
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
    super.dispose();
  }

  Future<void> _validateKey() async {
    final key = _keyController.text.trim();
    if (key.isEmpty) {
      setState(() {
        _keyState = KeyValidationState.invalid;
        _keyError = 'Enter an API key first.';
      });
      return;
    }
    setState(() {
      _keyState = KeyValidationState.validating;
      _keyError = null;
    });
    // Snapshot the provider: validation can take up to 90 s and the
    // dropdown stays enabled — a mid-flight switch must not persist this
    // key into the NEWLY selected provider's slot (and then show Key OK
    // for a key that belongs to a different service).
    final providerAtStart = widget.settings.provider;
    final error = await widget.analyzer.validateKey(key); // null = OK
    if (!mounted) return;
    if (widget.settings.provider != providerAtStart) {
      setState(() => _keyState = KeyValidationState.idle);
      return; // stale validation: neither persist nor report
    }
    if (error == null) {
      await widget.settings.update(apiKey: key);
      if (!mounted) return;
      setState(() => _keyState = KeyValidationState.valid);
    } else {
      setState(() {
        _keyState = KeyValidationState.invalid;
        _keyError = error;
      });
    }
  }

  Future<void> _toggleWatcher(bool enable) async {
    if (enable) {
      final granted = await widget.requestPhotoPermission();
      if (!granted) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
            content: Text(
                'Photo library permission is required for automatic intake.')));
        return; // leave the switch off
      }
      await widget.photoIntake?.start();
      // Instant feedback on enable: sweep the lookback window right away
      // instead of waiting for the next change event / background run.
      // Only when analyses can actually succeed (key present, no quota
      // pause) — otherwise the sweep reads bytes for nothing.
      if (widget.settings.apiKey.trim().isNotEmpty &&
          !widget.settings.isQuotaPaused) {
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

  Future<void> _export() async {
    setState(() => _exporting = true);
    try {
      final json = await widget.dao.exportJson(); // spec §8 full export
      await SharePlus.instance.share(
          ShareParams(text: json, subject: 'CalorieTracker data export'));
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
        _ => 'Default: gemini-2.5-flash',
      };

  Widget _keyStatusIcon() {
    switch (_keyState) {
      case KeyValidationState.idle:
        return const SizedBox.shrink();
      case KeyValidationState.validating:
        return const Padding(
          padding: EdgeInsets.all(12),
          child: SizedBox(
            key: Key('keyValidating'),
            width: 18,
            height: 18,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
        );
      case KeyValidationState.valid:
        return const Icon(Icons.check_circle,
            key: Key('keyValid'), color: Colors.green);
      case KeyValidationState.invalid:
        return Icon(Icons.error,
            key: const Key('keyInvalid'),
            color: Theme.of(context).colorScheme.error);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text('Gemini', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        DropdownButtonFormField<String>(
          key: const Key('providerDropdown'),
          initialValue: widget.settings.provider,
          decoration: const InputDecoration(
            labelText: 'AI provider',
            helperText: 'Key and model below belong to the selected '
                'provider. "My server" analyses run on your own server '
                'under your Claude subscription — no API charges.',
            border: OutlineInputBorder(),
          ),
          items: const [
            DropdownMenuItem(value: 'gemini', child: Text('Google Gemini')),
            DropdownMenuItem(value: 'openai', child: Text('OpenAI')),
            DropdownMenuItem(
                value: 'anthropic', child: Text('Anthropic Claude')),
            DropdownMenuItem(
                value: 'server',
                child: Text('My server (Claude subscription)')),
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
              _keyState = KeyValidationState.idle;
              _keyError = null;
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
                  'with your Claude subscription.',
              border: OutlineInputBorder(),
            ),
            onSubmitted: (v) =>
                widget.settings.update(serverBaseUrl: v.trim()),
            onTapOutside: (_) => widget.settings
                .update(serverBaseUrl: _serverUrlController.text.trim()),
          ),
          const SizedBox(height: 12),
        ],
        TextField(
          key: const Key('apiKeyField'),
          controller: _keyController,
          obscureText: true,
          autocorrect: false,
          onChanged: (_) {
            if (_keyState != KeyValidationState.idle) {
              setState(() {
                _keyState = KeyValidationState.idle;
                _keyError = null;
              });
            }
          },
          decoration: InputDecoration(
            labelText: _isServerProvider
                ? 'Server upload key'
                : '${_providerLabel()} API key',
            helperText: _isServerProvider
                ? 'The X-API-Key your server expects. Stored securely on '
                    'this device only.'
                : 'Stored securely on this device only.',
            border: const OutlineInputBorder(),
            suffixIcon: _keyStatusIcon(),
          ),
        ),
        if (_keyError != null)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              _keyError!,
              key: const Key('keyErrorText'),
              style: TextStyle(color: theme.colorScheme.error),
            ),
          ),
        if (_keyState == KeyValidationState.valid)
          const Padding(
            padding: EdgeInsets.only(top: 6),
            child: Text('Key OK', key: Key('keyOkText')),
          ),
        const SizedBox(height: 8),
        Align(
          alignment: Alignment.centerLeft,
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              FilledButton.tonal(
                key: const Key('validateKeyButton'),
                onPressed: _keyState == KeyValidationState.validating
                    ? null
                    : _validateKey,
                child: Text(
                    _isServerProvider ? 'Test connection' : 'Validate key'),
              ),
              if (_isServerProvider &&
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
        if (!_isServerProvider)
          TextField(
            key: const Key('modelField'),
            controller: _modelController,
            autocorrect: false,
            onSubmitted: (v) => widget.settings.update(model: v.trim()),
            // Tap-away must not lose the edit — onSubmitted only fires on
            // the keyboard action key.
            onTapOutside: (_) =>
                widget.settings.update(model: _modelController.text.trim()),
            decoration: InputDecoration(
              labelText: 'Model',
              helperText: _modelHelperText(),
              border: const OutlineInputBorder(),
            ),
          ),
        const Divider(height: 32),
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
        const Divider(height: 32),
        Text('Daily report', style: theme.textTheme.titleMedium),
        ListTile(
          key: const Key('reportTimeTile'),
          contentPadding: EdgeInsets.zero,
          title: const Text('Report time'),
          trailing: Text(_reportTime, style: theme.textTheme.titleMedium),
          onTap: _pickReportTime,
        ),
        const Divider(height: 32),
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
        const Divider(height: 32),
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
        const SizedBox(height: 24),
      ],
    );
  }
}
