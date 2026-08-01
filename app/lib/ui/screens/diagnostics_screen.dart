/// The "why doesn't my AI work" page (user request 2026-07-31). One tap
/// runs the staged checks in ui/diagnostics.dart and renders each verdict
/// with the evidence and the user's next move — the debugging session this
/// replaces took an hour of server-log archaeology.
library;

import 'package:flutter/material.dart';

import '../diagnostics.dart';

class DiagnosticsScreen extends StatefulWidget {
  final ProviderDiagnostics diagnostics;
  const DiagnosticsScreen({super.key, required this.diagnostics});

  @override
  State<DiagnosticsScreen> createState() => _DiagnosticsScreenState();
}

class _DiagnosticsScreenState extends State<DiagnosticsScreen> {
  final List<DiagResult> _results = [];
  bool _running = false;
  bool _ran = false;

  Future<void> _run() async {
    setState(() {
      _running = true;
      _results.clear();
    });
    try {
      await for (final r in widget.diagnostics.run()) {
        if (!mounted) return;
        setState(() => _results.add(r));
      }
    } catch (e) {
      // A stage that throws must not leave a half-finished run wearing the
      // green "Everything works" verdict — that is the exact false
      // reassurance this page exists to prevent.
      if (mounted) {
        setState(() => _results.add(DiagResult(
              'Test run',
              DiagStatus.fail,
              'The check itself failed part-way through.',
              detail: '$e',
              fix: 'Re-run; if it keeps failing here, the provider is '
                  'answering something the app cannot parse at all.',
            )));
      }
    } finally {
      if (mounted) {
        setState(() {
          _running = false;
          _ran = true;
        });
      }
    }
  }

  (IconData, Color) _iconFor(DiagStatus s, ThemeData theme) => switch (s) {
        DiagStatus.pass => (Icons.check_circle, Colors.green),
        DiagStatus.warn => (Icons.warning_amber_rounded, Colors.orange),
        DiagStatus.fail => (Icons.error, theme.colorScheme.error),
      };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final problems =
        _results.where((r) => r.status != DiagStatus.pass).length;
    return Scaffold(
      appBar: AppBar(title: const Text('Test AI provider')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(
            'Checks your AI setup step by step and names exactly what is '
            'broken: configuration, network (VPN), key, account credit, '
            'reply format, and quota. Running the test spends two small '
            'AI calls.',
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
          ),
          const SizedBox(height: 12),
          FilledButton.icon(
            key: const Key('runDiagnostics'),
            onPressed: _running ? null : _run,
            icon: _running
                ? const SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(strokeWidth: 2))
                : const Icon(Icons.play_arrow),
            label: Text(_running
                ? 'Testing…'
                : _ran
                    ? 'Run again'
                    : 'Run the checks'),
          ),
          if (_ran && !_running) ...[
            const SizedBox(height: 12),
            Card(
              key: const Key('diagVerdict'),
              color: problems == 0
                  ? theme.colorScheme.secondaryContainer
                  : theme.colorScheme.errorContainer,
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Text(
                  problems == 0
                      ? 'Everything works. If a photo still fails, it is '
                          'photo-specific — try "Analyze again" on it.'
                      : '$problems problem${problems == 1 ? '' : 's'} '
                          'found — the red/orange rows below say what to '
                          'do.',
                  style: theme.textTheme.titleSmall,
                ),
              ),
            ),
          ],
          const SizedBox(height: 4),
          for (final r in _results) ...[
            const SizedBox(height: 8),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Icon(_iconFor(r.status, theme).$1,
                            size: 20, color: _iconFor(r.status, theme).$2),
                        const SizedBox(width: 8),
                        Expanded(
                            child: Text(r.stage,
                                style: theme.textTheme.titleSmall)),
                      ],
                    ),
                    const SizedBox(height: 6),
                    Text(r.summary, style: theme.textTheme.bodyMedium),
                    if (r.detail != null) ...[
                      const SizedBox(height: 4),
                      Text(r.detail!,
                          style: theme.textTheme.bodySmall?.copyWith(
                              color: theme.colorScheme.onSurfaceVariant)),
                    ],
                    if (r.fix != null) ...[
                      const SizedBox(height: 6),
                      Text('→ ${r.fix!}',
                          style: theme.textTheme.bodySmall?.copyWith(
                              fontWeight: FontWeight.w600)),
                    ],
                  ],
                ),
              ),
            ),
          ],
          if (_running) ...[
            const SizedBox(height: 16),
            const Center(
                child: CircularProgressIndicator(
                    key: Key('diagRunning'))),
          ],
        ],
      ),
    );
  }
}
