/// Onboarding/Settings (spec §8 knobs), in six section builders: AI
/// provider + key + curated model picker + one "Test this provider"
/// action (the diagnostics page), photo intake (watcher, coverage audit,
/// backfill window), daily-report time, dietary profile, and the data
/// section (export to a file, import by merge).
library;

import 'dart:async' show unawaited;

import 'dart:io';

import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';

import '../../core/contracts.dart';
import 'package:flutter/foundation.dart' show compute;

import '../../services/analyzer/normalize.dart' show makeMealThumb;
import '../../services/photo/coverage.dart';
import '../../services/photo/photo_library.dart';
import '../photo_pipeline.dart';
import '../format.dart' show isoDate;
import '../services.dart';
import '../widgets/grouped.dart';
import 'coverage_screen.dart';
import 'meal_editor_screen.dart';
import 'settings/profile_page.dart';
import 'settings/provider_page.dart';

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
  late final TextEditingController _profileController;
  final TextEditingController _importController = TextEditingController();
  late int _lookbackDays;
  late String _reportTime;
  late bool _watcherEnabled;
  bool _exporting = false;
  bool _importing = false;

  @override
  void initState() {
    super.initState();
    final s = widget.settings;
    _profileController = TextEditingController(text: s.dietaryProfile);
    _lookbackDays = s.lookbackDays.clamp(1, 30); // spec §6.4 range
    _reportTime = s.reportTime.isEmpty ? '21:00' : s.reportTime;
    _watcherEnabled = s.watcherEnabled;
  }

  @override
  void dispose() {
    _profileController.dispose();
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
      // A "selected photos" (limited) grant is a TRAP: the watcher can
      // only ever see the photos picked in that one dialog, so a meal
      // shot later is invisible forever — and the toggle used to turn on
      // regardless, silently. Say so, with a path to fix it.
      final lib = widget.photoLibrary;
      if (lib != null && !await lib.hasFullAccess()) {
        if (!mounted) return;
        final open = widget.openSystemSettings;
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            duration: const Duration(seconds: 8),
            content: const Text(
                'Only SELECTED photos are shared, so new food photos '
                "won't be seen automatically. Grant access to ALL photos "
                'for automatic logging.'),
            action: open == null
                ? null
                : SnackBarAction(
                    label: 'Fix', onPressed: () => open())));
      }
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
      // Sweep older exports first: each is a FULL copy of the food log,
      // and the cache dir is readable by anything with the app's storage
      // — keeping a year of them there is a privacy cost with no upside.
      try {
        for (final f in dir.listSync()) {
          if (f is File &&
              f.path.split('/').last.startsWith('calorietracker-')) {
            f.deleteSync();
          }
        }
      } catch (_) {}
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


  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    // Apple grouped anatomy (2026-08-02 restructure): inset sections on a
    // grouped background, disclosure to subpages, footers as helper text.
    // Section builders keep features findable — the wall is gone.
    return Container(
      color: groupedBackground(theme.colorScheme),
      child: CustomScrollView(
        slivers: [
          const SliverAppBar.large(title: Text('Settings')),
          SliverList.list(children: [
            ..._aiSection(theme),
            ..._photoSection(theme),
            ..._reportSection(theme),
            ..._profileSection(theme),
            ..._dataSection(theme),
            const SizedBox(height: 32),
          ]),
        ],
      ),
    );
  }

  /// One row of STATE (Apple progressive disclosure): choosing and
  /// configuring the provider lives on its own page.
  List<Widget> _aiSection(ThemeData theme) => [
        // First-run: the shell deliberately lands a key-less install here.
        // Everything a mainland user needs to know is one tap away on the
        // provider page; this card just points there.
        if (widget.settings.apiKey.trim().isEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 0),
            child: Card(
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
                      'Tap AI Provider below, pick a provider and paste '
                      'its API key (it saves as you type), then Test This '
                      'Provider.\n'
                      'Then turn on Watch Camera Roll and new food photos '
                      'log themselves.\n'
                      'In mainland China choose Qwen 通义千问, Doubao 豆包 or '
                      'GLM 智谱 (GLM\u2019s default model is free) — the other '
                      'providers need a VPN. 中国大陆用户请选择国内提供商。',
                      style: theme.textTheme.bodySmall,
                    ),
                  ],
                ),
              ),
            ),
          ),
        GroupedSection(
          header: 'AI',
          footer: widget.settings.isQuotaPaused
              ? 'Analyses are paused — the daily quota was hit. New photos '
                  'are kept and retried automatically; changing the key or '
                  'provider on the AI Provider page resumes now.'
              : 'Photos are analysed by the provider you pick — its key '
                  'never leaves this phone.',
          children: [
            GroupedRow(
              key: const Key('aiProviderRow'),
              icon: Icons.auto_awesome,
              iconColor: theme.colorScheme.primary,
              title: 'AI Provider',
              value: providerLabel(widget.settings.provider),
              onTap: () async {
                await Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) => ProviderSettingsPage(
                    settings: widget.settings,
                    analyzer: widget.analyzer,
                    startClaudeAuth: widget.startClaudeAuth,
                    completeClaudeAuth: widget.completeClaudeAuth,
                    openUrl: widget.openUrl,
                  ),
                ));
                if (mounted) setState(() {}); // provider label refresh
              },
            ),
          ],
        ),
  ];

  /// Camera-roll watching, coverage audit, backfill window.
  List<Widget> _photoSection(ThemeData theme) => [
        GroupedSection(
          header: 'Photos',
          footer: 'The watcher logs new food photos automatically; the '
              'lookback window decides how far back catch-up scans reach.',
          children: [
            GroupedToggleRow(
              switchKey: const Key('watcherToggle'),
              icon: Icons.photo_camera_outlined,
              iconColor: theme.colorScheme.primary,
              title: 'Watch Camera Roll',
              value: _watcherEnabled,
              onChanged: (v) => _toggleWatcher(v),
            ),
            GroupedRow(
              icon: Icons.history,
              iconColor: theme.colorScheme.tertiary,
              title: 'Backfill Lookback',
              value: '$_lookbackDays day${_lookbackDays == 1 ? '' : 's'}',
              onTap: _pickLookback,
            ),
            if (widget.coverage != null && widget.processPhoto != null)
              GroupedRow(
                key: const Key('coverageCheckTile'),
                icon: Icons.fact_check_outlined,
                iconColor: theme.colorScheme.secondary,
                title: 'Photo Coverage',
                onTap: () => Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) => CoverageScreen(
                    auditor: widget.coverage!,
                    processPhoto: widget.processPhoto!,
                    requestPhotoPermission: widget.requestPhotoPermission,
                    initialLookbackDays: _lookbackDays,
                    // A closure, not a snapshot: the quota latch can arm
                    // while the coverage screen's bulk run is mid-batch.
                    canAnalyze: () => widget.settings.canAnalyze,
                    library: widget.photoLibrary,
                    logManually: (photo) async =>
                        await Navigator.of(context)
                            .push<bool>(MaterialPageRoute(
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
          ],
        ),
  ];

  /// Apple's compact-value pattern: the row shows the state; a sheet
  /// adjusts it (the old always-visible slider gave a rarely-touched
  /// setting permanent screen space).
  Future<void> _pickLookback() async {
    var days = _lookbackDays;
    final picked = await showModalBottomSheet<int>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setSheet) => Padding(
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('Backfill lookback',
                  style: Theme.of(ctx).textTheme.titleMedium),
              Text('How many days catch-up scans look back.',
                  style: Theme.of(ctx).textTheme.bodySmall),
              Slider(
                key: const Key('lookbackSlider'),
                value: days.toDouble(),
                min: 1,
                max: 30, // spec §6.4 clamp 1–30
                divisions: 29,
                label: '$days',
                onChanged: (v) => setSheet(() => days = v.round()),
              ),
              Align(
                alignment: Alignment.centerRight,
                child: FilledButton(
                  onPressed: () => Navigator.pop(ctx, days),
                  child: Text('Set $days day${days == 1 ? '' : 's'}'),
                ),
              ),
            ],
          ),
        ),
      ),
    );
    if (picked == null || !mounted) return;
    setState(() => _lookbackDays = picked);
    await widget.settings.update(lookbackDays: picked);
  }

  /// Daily-report time.
  List<Widget> _reportSection(ThemeData theme) => [
        GroupedSection(
          header: 'Report',
          children: [
            GroupedRow(
              key: const Key('reportTimeTile'),
              icon: Icons.schedule,
              iconColor: theme.colorScheme.primary,
              title: 'Report Time',
              value: _reportTime,
              onTap: _pickReportTime,
            ),
          ],
        ),
  ];

  /// Dietary profile appended to the photo prompt (§1.3).
  List<Widget> _profileSection(ThemeData theme) => [
        GroupedSection(
          header: 'Profile',
          children: [
            GroupedRow(
              key: const Key('dietaryProfileRow'),
              icon: Icons.person_outline,
              iconColor: theme.colorScheme.secondary,
              title: 'Dietary Profile',
              value: widget.settings.dietaryProfile.trim().isEmpty
                  ? 'Not set'
                  : 'Set',
              onTap: () async {
                await Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) =>
                      ProfileSettingsPage(settings: widget.settings),
                ));
                if (mounted) setState(() {}); // Set/Not set refresh
              },
            ),
          ],
        ),
  ];

  /// Export and import — the food log is the user's.
  List<Widget> _dataSection(ThemeData theme) => [
        GroupedSection(
          header: 'Your Data',
          footer: 'Import MERGES an exported file into this phone: meals '
              'already here are left alone, so importing twice never '
              'doubles your calories.',
          children: [
            GroupedRow(
              key: const Key('exportButton'),
              icon: Icons.ios_share,
              iconColor: theme.colorScheme.primary,
              title: 'Export Data…',
              trailing: _exporting
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : null,
              showChevron: false,
              onTap: _exporting ? null : _export,
            ),
            GroupedRow(
              key: const Key('importButton'),
              icon: Icons.file_download_outlined,
              iconColor: theme.colorScheme.secondary,
              title: 'Import Data…',
              trailing: _importing
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : null,
              showChevron: false,
              onTap: _importing ? null : _import,
            ),
          ],
        ),
  ];
}

