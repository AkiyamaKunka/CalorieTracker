/// The Subscription connection type (user-designed IA, 2026-08-02 v2:
/// each connection type gets its OWN page). Three flat-rate plans as a
/// checkmark list; picking one selects the server route AND that plan.
/// The server config (address, upload key, Claude connect) appears once
/// a plan is active.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show HapticFeedback;
import 'package:url_launcher/url_launcher.dart' as launcher;

import '../../services.dart' show SettingsStore;
import '../../l10n.dart';
import '../../widgets/grouped.dart';
import 'provider_page.dart' show kPlanChoices;

class SubscriptionProviderPage extends StatefulWidget {
  const SubscriptionProviderPage({
    super.key,
    required this.settings,
    this.startClaudeAuth,
    this.completeClaudeAuth,
    this.openUrl,
  });

  final SettingsStore settings;
  final Future<({String? url, String? error})> Function()? startClaudeAuth;
  final Future<String?> Function(String code)? completeClaudeAuth;
  final Future<bool> Function(Uri url)? openUrl;

  @override
  State<SubscriptionProviderPage> createState() =>
      _SubscriptionProviderPageState();
}

class _SubscriptionProviderPageState
    extends State<SubscriptionProviderPage> {
  late final TextEditingController _keyController;
  late final TextEditingController _serverUrlController;
  bool _authBusy = false;

  bool get _planActive => widget.settings.provider == 'server';

  @override
  void initState() {
    super.initState();
    _keyController = TextEditingController(
        text: _planActive ? widget.settings.apiKey : '');
    _serverUrlController =
        TextEditingController(text: widget.settings.serverBaseUrl);
  }

  @override
  void dispose() {
    _keyController.dispose();
    _serverUrlController.dispose();
    super.dispose();
  }

  /// Picking a PLAN means: provider = the server, backend = that plan.
  Future<void> _selectPlan(String backend) async {
    final already = _planActive &&
        widget.settings.serverBackend == backend;
    if (already) return;
    HapticFeedback.selectionClick();
    await widget.settings.update(serverBackend: backend);
    if (widget.settings.provider != 'server') {
      await widget.settings.update(provider: 'server');
    }
    if (!mounted) return;
    setState(() {
      _keyController.text = widget.settings.apiKey;
    });
  }

  /// The phone half of the server's Claude OAuth re-connect: fetch the
  /// OFFICIAL Anthropic authorize URL from our server, open it in the
  /// system browser (RFC 8252: never an in-app login form), then collect
  /// the pasted code and hand it back. No credential touches this device.
  Future<void> _connectClaude() async {
    final start = widget.startClaudeAuth;
    final complete = widget.completeClaudeAuth;
    if (start == null || complete == null) return;
    setState(() => _authBusy = true);
    try {
      final started = await start();
      if (!mounted) return;
      if (started.error != null || started.url == null) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content:
                Text(started.error ?? context.l10n.connectStartFailed)));
        return;
      }
      final open = widget.openUrl ??
          (uri) => launcher.launchUrl(uri,
              mode: launcher.LaunchMode.externalApplication);
      final opened = await open(Uri.parse(started.url!));
      if (!mounted) return;
      if (!opened) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
            content: Text(context.l10n.connectBrowserFailed)));
        return;
      }
      final code = await showDialog<String>(
        context: context,
        barrierDismissible: false,
        builder: (ctx) {
          final ctrl = TextEditingController();
          return AlertDialog(
            title: Text(ctx.l10n.connectDialogTitle),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(ctx.l10n.connectDialogBody),
                const SizedBox(height: 12),
                TextField(
                  key: const Key('claudeAuthCodeField'),
                  controller: ctrl,
                  autocorrect: false,
                  decoration: InputDecoration(
                    labelText: ctx.l10n.connectCodeLabel,
                    border: const OutlineInputBorder(),
                  ),
                ),
              ],
            ),
            actions: [
              TextButton(
                  onPressed: () => Navigator.of(ctx).pop(null),
                  child: Text(ctx.l10n.cancel)),
              FilledButton(
                key: const Key('claudeAuthSubmit'),
                onPressed: () => Navigator.of(ctx).pop(ctrl.text.trim()),
                child: Text(ctx.l10n.connect),
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
          content: Text(
              error ?? context.l10n.connectDone)));
    } finally {
      if (mounted) setState(() => _authBusy = false);
    }
  }

  Widget _cellField(Widget field) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
        child: field,
      );

  String _modelLabel(BuildContext context, String v) => switch (v) {
        'fable' => 'Fable',
        'opus' => 'Opus',
        'sonnet' => 'Sonnet',
        'haiku' => 'Haiku',
        _ => context.l10n.planChoiceDefault,
      };

  String _effortLabel(BuildContext context, String v) => switch (v) {
        'low' => context.l10n.planEffortLow,
        'medium' => context.l10n.planEffortMedium,
        'high' => context.l10n.planEffortHigh,
        _ => context.l10n.planChoiceDefault,
      };

  /// One picker for both rows: the sheet lists (value, label) options
  /// with a checkmark on the current one; '' = server default.
  Future<void> _pickPlanValue({
    required String key,
    required String title,
    required String current,
    required List<(String, String)> options,
    required Future<void> Function(String) apply,
  }) async {
    final picked = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      backgroundColor: groupedBackground(Theme.of(context).colorScheme),
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 4),
              child: Align(
                alignment: Alignment.centerLeft,
                child: Text(title,
                    style: Theme.of(ctx)
                        .textTheme
                        .titleLarge
                        ?.copyWith(fontWeight: FontWeight.w600)),
              ),
            ),
            GroupedSection(
              separatorInset: 16,
              children: [
                for (final (value, label) in options)
                  GroupedRow(
                    key: Key('plan-$key-${value.isEmpty ? 'default' : value}'),
                    title: label,
                    showChevron: false,
                    trailing: current == value
                        ? Icon(Icons.check,
                            size: 20,
                            color: Theme.of(ctx).colorScheme.primary)
                        : const SizedBox(width: 20),
                    onTap: () => Navigator.pop(ctx, value),
                  ),
              ],
            ),
            const SizedBox(height: 16),
          ],
        ),
      ),
    );
    if (picked == null || !mounted) return;
    await apply(picked);
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final settings = widget.settings;
    return GroupedPage(
      title: context.l10n.subPageTitle,
      children: [
        GroupedSection(
          header: context.l10n.planHeader,
          footer: context.l10n.planFooter,
          children: [
            for (final (backend, name, note) in kPlanChoices)
              GroupedRow(
                key: Key('planChoice-$backend'),
                title: name,
                value: note,
                showChevron: false,
                trailing: _planActive &&
                        settings.serverBackend == backend
                    ? Icon(Icons.check, size: 20, color: scheme.primary)
                    : const SizedBox(width: 20),
                onTap: () => _selectPlan(backend),
              ),
          ],
        ),
        if (_planActive) ...[
          GroupedSection(
            header: context.l10n.serverHeader,
            footer: context.l10n.serverFooter,
            children: [
              _cellField(TextField(
                key: const Key('serverUrlField'),
                controller: _serverUrlController,
                autocorrect: false,
                keyboardType: TextInputType.url,
                decoration: InputDecoration(
                  labelText: context.l10n.serverAddressLabel,
                  hintText: 'http://your.server.ip',
                  border: InputBorder.none,
                ),
                onSubmitted: (v) =>
                    settings.update(serverBaseUrl: v.trim()),
                onTapOutside: (_) => settings.update(
                    serverBaseUrl: _serverUrlController.text.trim()),
              )),
              _cellField(TextField(
                key: const Key('serverUploadKeyField'),
                controller: _keyController,
                obscureText: true,
                autocorrect: false,
                onChanged: (v) => settings.update(apiKey: v.trim()),
                onSubmitted: (v) => settings.update(apiKey: v.trim()),
                onTapOutside: (_) =>
                    settings.update(apiKey: _keyController.text.trim()),
                decoration: InputDecoration(
                  labelText: context.l10n.serverUploadKeyLabel,
                  border: InputBorder.none,
                ),
              )),
            ],
          ),
          if (settings.serverBackend == 'claude')
            GroupedSection(
              header: context.l10n.planTuningHeader,
              footer: context.l10n.planTuningFooter,
              children: [
                GroupedRow(
                  key: const Key('planModelRow'),
                  icon: Icons.auto_awesome_outlined,
                  iconColor: scheme.primary,
                  title: context.l10n.planModelRow,
                  value: _modelLabel(context, settings.serverModel),
                  onTap: () => _pickPlanValue(
                    key: 'model',
                    title: context.l10n.planModelRow,
                    current: settings.serverModel,
                    options: [
                      ('', context.l10n.planChoiceDefault),
                      ('fable', context.l10n.planModelFable),
                      ('opus', context.l10n.planModelOpus),
                      ('sonnet', context.l10n.planModelSonnet),
                      ('haiku', context.l10n.planModelHaiku),
                    ],
                    apply: (v) => settings.update(serverModel: v),
                  ),
                ),
                GroupedRow(
                  key: const Key('planEffortRow'),
                  icon: Icons.psychology_outlined,
                  iconColor: scheme.secondary,
                  title: context.l10n.planEffortRow,
                  value: _effortLabel(context, settings.serverEffort),
                  onTap: () => _pickPlanValue(
                    key: 'effort',
                    title: context.l10n.planEffortRow,
                    current: settings.serverEffort,
                    options: [
                      ('', context.l10n.planChoiceDefault),
                      ('low', context.l10n.planEffortLow),
                      ('medium', context.l10n.planEffortMedium),
                      ('high', context.l10n.planEffortHigh),
                    ],
                    apply: (v) => settings.update(serverEffort: v),
                  ),
                ),
              ],
            ),
          if (settings.serverBackend == 'claude' &&
              widget.startClaudeAuth != null)
            GroupedSection(
              footer: context.l10n.connectClaudeFooter,
              children: [
                GroupedRow(
                  key: const Key('connectClaudeButton'),
                  icon: Icons.link,
                  iconColor: scheme.tertiary,
                  title: context.l10n.connectClaude,
                  trailing: _authBusy
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child:
                              CircularProgressIndicator(strokeWidth: 2))
                      : null,
                  onTap: _authBusy ? null : _connectClaude,
                ),
              ],
            ),
        ] else
          GroupedSection(
            footer: context.l10n.subInactiveFooter,
            children: const [SizedBox(height: 1)],
          ),
      ],
    );
  }
}
