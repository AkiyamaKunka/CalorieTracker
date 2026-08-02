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
                Text(started.error ?? 'Could not start the sign-in.')));
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

  Widget _cellField(Widget field) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
        child: field,
      );

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final settings = widget.settings;
    return GroupedPage(
      title: 'Subscription',
      children: [
        GroupedSection(
          header: 'Plan',
          footer: 'Flat-rate: your own cloud machine signs in to ONE plan '
              'and analyses photos under it — each photo costs nothing '
              'extra. Plan credentials stay on that machine.',
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
            header: 'Your server',
            footer: 'The cloud machine that holds your plan sign-in and '
                'runs the analysis — not this phone. This phone keeps '
                'only the upload key it uses to talk to that machine.',
            children: [
              _cellField(TextField(
                key: const Key('serverUrlField'),
                controller: _serverUrlController,
                autocorrect: false,
                keyboardType: TextInputType.url,
                decoration: const InputDecoration(
                  labelText: 'Server address',
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
                decoration: const InputDecoration(
                  labelText: 'Server upload key',
                  border: InputBorder.none,
                ),
              )),
            ],
          ),
          if (settings.serverBackend == 'claude' &&
              widget.startClaudeAuth != null)
            GroupedSection(
              footer: 'Signs this server in to your Anthropic '
                  'subscription. Needs a VPN in mainland China.',
              children: [
                GroupedRow(
                  key: const Key('connectClaudeButton'),
                  icon: Icons.link,
                  iconColor: scheme.tertiary,
                  title: 'Connect Claude',
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
            footer: 'An API key is currently active. Pick a plan above to '
                'switch to subscription analysis via your server.',
            children: const [SizedBox(height: 1)],
          ),
      ],
    );
  }
}
