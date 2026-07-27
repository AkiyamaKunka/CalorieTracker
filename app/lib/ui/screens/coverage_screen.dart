/// Photo-coverage check (spec §9 app-only): a MANUAL audit of "has every
/// photo in the window been through intake?", with one-tap remediation.
///
/// The audit itself never calls a model (it only hashes bytes and reads the
/// ledger); the Log/Retry actions push photos through the app's ONE
/// serialized pipeline, so they queue behind — never race — the watcher.
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../../services/photo/coverage.dart';
import '../../services/photo/photo_library.dart';
import '../photo_pipeline.dart';

class CoverageScreen extends StatefulWidget {
  const CoverageScreen({
    super.key,
    required this.auditor,
    required this.processPhoto,
    required this.requestPhotoPermission,
    required this.initialLookbackDays,
    this.library,
    this.logManually,
  });

  final CoverageAuditor auditor;
  final Future<PhotoOutcome> Function(IntakePhoto photo) processPhoto;
  final Future<bool> Function() requestPhotoPermission;
  final int initialLookbackDays;

  /// For missing-photo thumbnails; null degrades to icons.
  final PhotoLibrary? library;

  /// Open the manual editor for a photo the model refused. Null hides the
  /// action (tests / no editor context).
  final Future<bool> Function(IntakePhoto photo)? logManually;

  @override
  State<CoverageScreen> createState() => _CoverageScreenState();
}

enum _Phase { idle, scanning, done, acting }

class _CoverageScreenState extends State<CoverageScreen> {
  _Phase _phase = _Phase.idle;
  late int _days = widget.initialLookbackDays.clamp(1, 30);
  CoverageReport? _report;
  String? _error;
  int _progressDone = 0;
  int _progressTotal = 0;
  int _actedOn = 0;
  // FUTURE-cached: dedupes in-flight fetches across rebuilds (the bytes
  // cache alternative re-fired a platform call per rebuild until resolve).
  final Map<String, Future<Uint8List?>> _thumbCache = {};

  /// Tiles rendered per section; an audit can return hundreds of missing
  /// photos and building them all eagerly fires that many thumbnail
  /// fetches in one frame.
  static const int _maxTiles = 60;

  Future<void> _runAudit() async {
    final granted = await widget.requestPhotoPermission();
    if (!mounted) return;
    if (!granted) {
      setState(() => _error = 'Photo permission is required for the check.');
      return;
    }
    setState(() {
      _phase = _Phase.scanning;
      _error = null;
      _report = null;
      _progressDone = 0;
      _progressTotal = 0;
    });
    try {
      final report = await widget.auditor.audit(
        lookbackDays: _days,
        onProgress: (done, total) {
          if (!mounted) return;
          setState(() {
            _progressDone = done;
            _progressTotal = total;
          });
        },
      );
      if (!mounted) return;
      setState(() {
        _report = report;
        _phase = _Phase.done;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _phase = _Phase.idle;
        _error = 'Check failed: $e';
      });
    }
  }

  /// Push [items] through the pipeline one at a time, with progress. The
  /// screen re-audits afterwards so the summary reflects reality, not hope.
  Future<void> _processAll(List<CoverageItem> items, String label) async {
    setState(() {
      _phase = _Phase.acting;
      _actedOn = 0;
      _progressDone = 0;
      _progressTotal = items.length;
    });
    var failures = 0;
    for (final item in items) {
      final photo = await widget.auditor.loadForProcessing(item);
      if (photo == null) {
        failures++;
      } else {
        final outcome = await widget.processPhoto(photo);
        if (outcome.kind == PhotoOutcomeKind.failed) failures++;
      }
      if (!mounted) return;
      setState(() {
        _actedOn++;
        _progressDone = _actedOn;
      });
    }
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(failures == 0
            ? '$label done (${items.length} photos).'
            : '$label done — $failures of ${items.length} could not be '
                'processed (kept for retry).')));
    await _runAudit();
  }

  /// A "not food" tombstone is otherwise permanent: re-analysis repeats the
  /// verdict, so the only real remedy is letting the user enter the meal.
  Future<void> _logManually(CoverageItem item) async {
    final open = widget.logManually;
    if (open == null) return;
    final photo = await widget.auditor.loadForProcessing(item);
    if (!mounted) return;
    if (photo == null) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('That photo is no longer readable.')));
      return;
    }
    final saved = await open(photo);
    if (saved && mounted) await _runAudit();
  }

  Future<Uint8List?> _thumbFor(CoverageItem item) =>
      _thumbCache.putIfAbsent(item.assetId,
          () => widget.library?.thumbnailByAssetId(item.assetId) ??
              Future.value(null));

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final report = _report;
    final busy = _phase == _Phase.scanning || _phase == _Phase.acting;
    return Scaffold(
      appBar: AppBar(title: const Text('Photo coverage')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(
            'Checks every photo of the last $_days day(s) against the log: '
            'each one is fingerprinted and looked up — nothing is sent to '
            'the AI by the check itself.',
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: Slider(
                  key: const Key('coverageWindowSlider'),
                  value: _days.toDouble(),
                  min: 1,
                  max: 30,
                  divisions: 29,
                  label: '$_days d',
                  onChanged: busy
                      ? null
                      : (v) => setState(() => _days = v.round()),
                ),
              ),
              Text('$_days d'),
            ],
          ),
          FilledButton.icon(
            key: const Key('runCoverageCheck'),
            onPressed: busy ? null : _runAudit,
            icon: const Icon(Icons.fact_check_outlined),
            label: Text(_phase == _Phase.scanning
                ? 'Checking…'
                : _phase == _Phase.acting
                    ? 'Working…'
                    : 'Run check'),
          ),
          if (busy) ...[
            const SizedBox(height: 12),
            LinearProgressIndicator(
              key: const Key('coverageProgress'),
              value: _progressTotal == 0
                  ? null
                  : _progressDone / _progressTotal,
            ),
            const SizedBox(height: 4),
            Text('$_progressDone / $_progressTotal',
                style: theme.textTheme.bodySmall),
          ],
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(top: 12),
              child: Text(_error!,
                  key: const Key('coverageError'),
                  style: TextStyle(color: theme.colorScheme.error)),
            ),
          if (report != null) ...[
            const SizedBox(height: 16),
            _SummaryCard(report: report),
            if (report.missing.isNotEmpty) ...[
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: Text('Never scanned (${report.missing.length})',
                        style: theme.textTheme.titleSmall),
                  ),
                  FilledButton.tonal(
                    key: const Key('logAllMissing'),
                    onPressed: busy
                        ? null
                        : () => _processAll(report.missing, 'Logging'),
                    child: const Text('Log all'),
                  ),
                ],
              ),
              for (final item in report.missing.take(_maxTiles))
                _PhotoTile(item: item, thumb: _thumbFor(item)),
              if (report.missing.length > _maxTiles)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                      '…and ${report.missing.length - _maxTiles} more — '
                      '"Log all" still covers every one.',
                      style: theme.textTheme.bodySmall),
                ),
            ],
            if (report.skippedNonFood.isNotEmpty) ...[
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: Text(
                        'Judged "not food" (${report.skippedNonFood.length})',
                        style: theme.textTheme.titleSmall),
                  ),
                  // A 'skipped' tombstone is permanent for the automated
                  // path, so improved rules (e.g. now admitting takeout
                  // order screenshots) would never revisit these on their
                  // own. A DELIBERATE re-add reclaims skipped rows
                  // (spec §2.3) — which is exactly what this does.
                  FilledButton.tonal(
                    key: const Key('reanalyzeAllSkipped'),
                    onPressed: busy
                        ? null
                        : () => _processAll(
                            report.skippedNonFood, 'Re-analyzing'),
                    child: const Text('Analyze again'),
                  ),
                ],
              ),
              Text(
                  'Drinks, order screenshots and unusual dishes land here. '
                  '"Analyze again" re-asks the AI (useful after the rules '
                  'improve); tap a row to enter it yourself.',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
              for (final item in report.skippedNonFood.take(_maxTiles))
                _PhotoTile(
                  item: item,
                  thumb: _thumbFor(item),
                  onTap: widget.logManually == null
                      ? null
                      : () => _logManually(item),
                ),
              if (report.skippedNonFood.length > _maxTiles)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                      '…and ${report.skippedNonFood.length - _maxTiles} more.',
                      style: theme.textTheme.bodySmall),
                ),
            ],
            if (report.failed.isNotEmpty) ...[
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: Text('Failed earlier (${report.failed.length})',
                        style: theme.textTheme.titleSmall),
                  ),
                  FilledButton.tonal(
                    key: const Key('retryAllFailed'),
                    onPressed: busy
                        ? null
                        : () => _processAll(report.failed, 'Retrying'),
                    child: const Text('Retry all'),
                  ),
                ],
              ),
              for (final item in report.failed.take(_maxTiles))
                _PhotoTile(item: item, thumb: _thumbFor(item)),
              if (report.failed.length > _maxTiles)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                      '…and ${report.failed.length - _maxTiles} more — '
                      '"Retry all" still covers every one.',
                      style: theme.textTheme.bodySmall),
                ),
            ],
          ],
        ],
      ),
    );
  }
}

class _SummaryCard extends StatelessWidget {
  const _SummaryCard({required this.report});
  final CoverageReport report;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final ok = report.fullyCovered;
    return Card(
      key: const Key('coverageSummary'),
      color: ok
          ? theme.colorScheme.secondaryContainer
          : theme.colorScheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(ok ? Icons.check_circle : Icons.info_outline,
                    size: 20,
                    color: ok
                        ? theme.colorScheme.onSecondaryContainer
                        : theme.colorScheme.onSurfaceVariant),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    ok
                        ? 'All ${report.scanned} photos are accounted for.'
                        : '${report.scanned} photos checked — '
                            '${report.missing.length} never scanned.',
                    style: theme.textTheme.titleSmall,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              [
                '${report.logged.length} logged as meals',
                '${report.skippedNonFood.length} not food',
                if (report.failed.isNotEmpty)
                    '${report.failed.length} failed',
                if (report.deleted > 0) '${report.deleted} deleted by you',
                if (report.inFlight > 0) '${report.inFlight} in progress',
                if (report.unreadable > 0)
                    '${report.unreadable} unreadable',
                if (report.tooLargeToAnalyze > 0)
                    '${report.tooLargeToAnalyze} too large to analyze',
              ].join(' · '),
              style: theme.textTheme.bodySmall,
            ),
            if (report.limitedAccess)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  'Note: the app has LIMITED photo access — only the '
                  'selected photos can be checked. Grant full access in '
                  'system settings for a complete answer.',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.error),
                ),
              ),
            if (report.truncated)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  'Note: over 2000 photos in this window — older ones were '
                  'not checked. Shorten the window for a complete answer.',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.error),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _PhotoTile extends StatelessWidget {
  const _PhotoTile({required this.item, required this.thumb, this.onTap});
  final CoverageItem item;
  final Future<Uint8List?> thumb;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListTile(
      onTap: onTap,
      trailing: onTap == null
          ? null
          : const Icon(Icons.edit_outlined, size: 18),
      contentPadding: EdgeInsets.zero,
      leading: FutureBuilder<Uint8List?>(
        future: thumb,
        builder: (context, snap) {
          final bytes = snap.data;
          if (bytes == null || bytes.isEmpty) {
            return Container(
              width: 44,
              height: 44,
              decoration: BoxDecoration(
                color: theme.colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(8),
              ),
              child: Icon(Icons.photo_outlined,
                  size: 20, color: theme.colorScheme.onSurfaceVariant),
            );
          }
          return ClipRRect(
            borderRadius: BorderRadius.circular(8),
            child: Image.memory(bytes,
                width: 44, height: 44, fit: BoxFit.cover),
          );
        },
      ),
      title: Text(item.fileName.isEmpty ? '(unnamed photo)' : item.fileName,
          style: theme.textTheme.bodyMedium, overflow: TextOverflow.ellipsis),
      subtitle: Text(
          '${item.createDate.year}-'
          '${item.createDate.month.toString().padLeft(2, '0')}-'
          '${item.createDate.day.toString().padLeft(2, '0')} '
          '${item.createDate.hour.toString().padLeft(2, '0')}:'
          '${item.createDate.minute.toString().padLeft(2, '0')}',
          style: theme.textTheme.bodySmall),
    );
  }
}
