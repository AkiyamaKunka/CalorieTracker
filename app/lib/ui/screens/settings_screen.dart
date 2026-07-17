/// Onboarding/Settings (spec §8 knobs): Gemini key with live validateKey
/// feedback, model, backfill-lookback slider, report time, watcher toggle
/// (requests photo permission), dietary profile, JSON export via share_plus.
library;

import 'package:flutter/material.dart';
import 'package:share_plus/share_plus.dart';

import '../../core/contracts.dart';
import '../services.dart';

enum KeyValidationState { idle, validating, valid, invalid }

class SettingsScreen extends StatefulWidget {
  final SettingsStore settings;
  final AnalyzerService analyzer;
  final MealsDao dao;
  final Future<bool> Function() requestPhotoPermission;
  final PhotoIntake? photoIntake; // started/stopped on watcher toggle

  const SettingsScreen({
    super.key,
    required this.settings,
    required this.analyzer,
    required this.dao,
    required this.requestPhotoPermission,
    this.photoIntake,
  });

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  late final TextEditingController _keyController;
  late final TextEditingController _modelController;
  late final TextEditingController _profileController;
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
    _lookbackDays = s.lookbackDays.clamp(1, 30); // spec §6.4 range
    _reportTime = s.reportTime.isEmpty ? '21:00' : s.reportTime;
    _watcherEnabled = s.watcherEnabled;
  }

  @override
  void dispose() {
    _keyController.dispose();
    _modelController.dispose();
    _profileController.dispose();
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
    final error = await widget.analyzer.validateKey(key); // null = OK
    if (!mounted) return;
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
            labelText: 'Gemini API key',
            helperText: 'Stored securely on this device only.',
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
          child: FilledButton.tonal(
            key: const Key('validateKeyButton'),
            onPressed: _keyState == KeyValidationState.validating
                ? null
                : _validateKey,
            child: const Text('Validate key'),
          ),
        ),
        const SizedBox(height: 12),
        TextField(
          key: const Key('modelField'),
          controller: _modelController,
          autocorrect: false,
          onSubmitted: (v) => widget.settings.update(model: v.trim()),
          decoration: const InputDecoration(
            labelText: 'Model',
            helperText: 'Default: gemini-2.5-flash',
            border: OutlineInputBorder(),
          ),
        ),
        const Divider(height: 32),
        Text('Photo intake', style: theme.textTheme.titleMedium),
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
