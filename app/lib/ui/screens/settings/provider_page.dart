/// The AI Provider page — Settings' first disclosure target (2026-08-02
/// Apple restructure). The root shows one row ("AI Provider — Gemini ›");
/// everything about CHOOSING and CONFIGURING a provider lives here, in
/// grouped sections: a checkmark list of the seven providers, the selected
/// provider's configuration cells, and the two actions.
///
/// The controls themselves are the battle-tested widgets lifted from the
/// old wall-of-forms screen — same Keys, same persistence semantics
/// (saves as you type; tap-away commits). What changed is the STRUCTURE:
/// progressive disclosure, footers instead of inline paragraphs, 44pt rows.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show HapticFeedback;
import 'package:url_launcher/url_launcher.dart' as launcher;

import '../../../core/contracts.dart';
import '../../diagnostics.dart';
import '../../services.dart' show SettingsStore;
import '../../widgets/grouped.dart';
import '../diagnostics_screen.dart';

/// Sentinel value for the model picker's "type it yourself" row.
const String kCustomModelSentinel = '__custom__';

/// Curated vision-capable models per provider (verified 2026-07 research),
/// with the tradeoff IN the label — users pick, they don't memorize vendor
/// id strings like doubao-seed-2-0-mini-260428. The Custom row reveals a
/// text field so a model a vendor ships NEXT month never bricks the app.
const Map<String, List<(String, String)>> kKnownModels = {
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

bool isCuratedModel(String provider, String model) =>
    (kKnownModels[provider] ?? const []).any((m) => m.$1 == model);

/// Short display name for a provider id — the root row's VALUE text.
String providerLabel(String provider) => switch (provider) {
      'openai' => 'OpenAI',
      'anthropic' => 'Claude',
      'server' => 'My server',
      'qwen' => 'Qwen 通义千问',
      'doubao' => 'Doubao 豆包',
      'glm' => 'GLM 智谱',
      _ => 'Gemini',
    };

/// The two CONNECTION TYPES (user-designed IA, 2026-08-02): an API key
/// is pay-per-photo with the key on this phone; an Agent/Coding plan is
/// a flat-rate subscription the user's own server signs into. Each type
/// lists its options; the note carries the one deciding fact.
const List<(String, String, String)> kApiKeyChoices = [
  ('gemini', 'Google Gemini', 'free tier · VPN in China'),
  ('openai', 'OpenAI', 'VPN in China'),
  ('anthropic', 'Anthropic Claude', 'VPN in China'),
  ('qwen', 'Alibaba Qwen 通义千问', '中国直连'),
  ('doubao', 'ByteDance Doubao 豆包', '中国直连'),
  ('glm', 'Zhipu GLM 智谱', 'free 免费 · 中国直连'),
];

/// (backend id, name, note) — all three ride the user's server.
const List<(String, String, String)> kPlanChoices = [
  ('claude', 'Claude Plan', 'Anthropic subscription'),
  ('glm', 'GLM Coding Plan', '¥49/mo'),
  ('doubao', 'Doubao Agent Plan', '¥40/mo'),
];

/// Root-row display: the concrete plan name, never an opaque 'My server'.
String providerDisplayLabel(String provider, String serverBackend) =>
    provider == 'server'
        ? switch (serverBackend) {
            'glm' => 'GLM Coding Plan',
            'doubao' => 'Doubao Agent Plan',
            _ => 'Claude Plan',
          }
        : providerLabel(provider);

class ProviderSettingsPage extends StatefulWidget {
  const ProviderSettingsPage({
    super.key,
    required this.settings,
    required this.analyzer,
    this.startClaudeAuth,
    this.completeClaudeAuth,
    this.openUrl,
  });

  final SettingsStore settings;
  final AnalyzerService analyzer;
  final Future<({String? url, String? error})> Function()? startClaudeAuth;
  final Future<String?> Function(String code)? completeClaudeAuth;
  final Future<bool> Function(Uri url)? openUrl;

  @override
  State<ProviderSettingsPage> createState() => _ProviderSettingsPageState();
}

class _ProviderSettingsPageState extends State<ProviderSettingsPage> {
  late final TextEditingController _keyController;
  late final TextEditingController _modelController;
  late final TextEditingController _serverUrlController;
  late bool _customModel;
  bool _authBusy = false;

  bool get _isServerProvider => widget.settings.provider == 'server';

  @override
  void initState() {
    super.initState();
    _keyController = TextEditingController(text: widget.settings.apiKey);
    _modelController = TextEditingController(text: widget.settings.model);
    _serverUrlController =
        TextEditingController(text: widget.settings.serverBaseUrl);
    _customModel =
        !isCuratedModel(widget.settings.provider, widget.settings.model);
  }

  @override
  void dispose() {
    _keyController.dispose();
    _modelController.dispose();
    _serverUrlController.dispose();
    super.dispose();
  }

  Future<void> _selectProvider(String id) async {
    if (id == widget.settings.provider) return;
    HapticFeedback.selectionClick();
    await widget.settings.update(provider: id);
    if (!mounted) return;
    setState(() {
      // Key/model fields are provider-scoped: reload them from the newly
      // selected provider's stored values.
      _keyController.text = widget.settings.apiKey;
      _modelController.text = widget.settings.model;
      _customModel =
          !isCuratedModel(widget.settings.provider, widget.settings.model);
    });
  }

  /// Picking a PLAN means: provider = the server, backend = that plan.
  Future<void> _selectPlan(String backend) async {
    final already = widget.settings.provider == 'server' &&
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
      _modelController.text = widget.settings.model;
      _customModel =
          !isCuratedModel(widget.settings.provider, widget.settings.model);
    });
  }

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

  /// Where the key comes from — Apple would say this in the FOOTER.
  String _keyFooter() => switch (widget.settings.provider) {
        'server' => 'The X-API-Key your server expects. Stored securely on '
            'this device only.',
        'qwen' => 'From bailian.console.aliyun.com (Alibaba Cloud 百炼 → '
            'API-KEY). New accounts get ~1M free tokens per model. Stored '
            'securely on this device only.',
        'doubao' => 'From console.volcengine.com/ark (API Key + 开通管理 to '
            'activate models). 500k free tokens per model. Stored securely '
            'on this device only.',
        'glm' => 'From open.bigmodel.cn (real-name verification required). '
            'The default flash model is free. Stored securely on this '
            'device only.',
        _ => 'The key saves as you type. Stored securely on this device '
            'only.',
      };

  /// A text field inside a grouped cell — Apple's inline-form pattern.
  Widget _cellField(Widget field) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
        child: field,
      );

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final settings = widget.settings;
    return GroupedPage(
      title: 'AI Provider',
      children: [
        if (settings.isQuotaPaused)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 0),
            child: Card(
              key: const Key('quotaPauseBanner'),
              color: scheme.errorContainer,
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Text(
                  'Analyses are paused — the daily quota was hit'
                  '${settings.quotaPauseUntil != null ? ' (until '
                      '${TimeOfDay.fromDateTime(settings.quotaPauseUntil!.toLocal()).format(context)})' : ''}. '
                  'New photos are kept and retried automatically; changing '
                  'the key or provider resumes now.',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: scheme.onErrorContainer),
                ),
              ),
            ),
          ),
        GroupedSection(
          header: 'API key · pay per photo',
          footer: 'The key lives on this phone; every photo is a metered '
              'API call billed by the vendor. In mainland China choose '
              'Qwen, Doubao or GLM — the others need a VPN. '
              '中国大陆用户请选择国内提供商。',
          children: [
            for (final (id, name, note) in kApiKeyChoices)
              GroupedRow(
                key: Key('providerChoice-$id'),
                title: name,
                value: note,
                showChevron: false,
                trailing: settings.provider == id
                    ? Icon(Icons.check, size: 20, color: scheme.primary)
                    : const SizedBox(width: 20),
                onTap: () => _selectProvider(id),
              ),
          ],
        ),
        GroupedSection(
          header: 'Subscription · via your server',
          footer: 'Your own cloud machine signs in to ONE flat-rate plan '
              'and analyses photos under it — each photo costs nothing '
              'extra. The plan credentials stay on that machine; this '
              'phone holds only your server’s upload key.',
          children: [
            for (final (backend, name, note) in kPlanChoices)
              GroupedRow(
                key: Key('planChoice-$backend'),
                title: name,
                value: note,
                showChevron: false,
                trailing: settings.provider == 'server' &&
                        settings.serverBackend == backend
                    ? Icon(Icons.check, size: 20, color: scheme.primary)
                    : const SizedBox(width: 20),
                onTap: () => _selectPlan(backend),
              ),
          ],
        ),
        if (_isServerProvider)
          GroupedSection(
            header: 'Your server',
            footer: 'The cloud machine that holds your plan sign-in and '
                'runs the analysis — not this phone.',
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
            ],
          ),
        GroupedSection(
          header: _isServerProvider ? 'Upload key' : 'API key',
          footer: _keyFooter(),
          children: [
            _cellField(TextField(
              key: const Key('apiKeyField'),
              controller: _keyController,
              obscureText: true,
              autocorrect: false,
              // Saves as you type — a paste-then-switch-tab can never
              // lose the key.
              onChanged: (v) => settings.update(apiKey: v.trim()),
              onSubmitted: (v) => settings.update(apiKey: v.trim()),
              onTapOutside: (_) =>
                  settings.update(apiKey: _keyController.text.trim()),
              decoration: InputDecoration(
                labelText: _isServerProvider
                    ? 'Server upload key'
                    : '${providerLabel(settings.provider)} API key',
                border: InputBorder.none,
              ),
            )),
          ],
        ),
        // The server path has no model cell: the VM's own
        // CLAUDE_ANALYZER_MODEL decides, and a phone-side value would be
        // a lie the user could not act on.
        if (!_isServerProvider)
          GroupedSection(
            header: 'Model',
            footer: _customModel ? _modelHelperText() : null,
            children: [
              _cellField(KeyedSubtree(
                key: ValueKey('modelPicker-${settings.provider}'),
                child: DropdownButtonFormField<String>(
                  key: const Key('modelPicker'),
                  isExpanded: true,
                  initialValue: _customModel
                      ? kCustomModelSentinel
                      : settings.model,
                  decoration:
                      const InputDecoration(border: InputBorder.none),
                  items: [
                    for (final (id, label)
                        in kKnownModels[settings.provider] ??
                            const <(String, String)>[])
                      DropdownMenuItem(value: id, child: Text(label)),
                    const DropdownMenuItem(
                        value: kCustomModelSentinel,
                        child: Text('Custom — type a model name…')),
                  ],
                  onChanged: (v) async {
                    if (v == null) return;
                    if (v == kCustomModelSentinel) {
                      setState(() => _customModel = true);
                      return;
                    }
                    await settings.update(model: v);
                    if (!mounted) return;
                    setState(() {
                      _customModel = false;
                      _modelController.text = v;
                    });
                  },
                ),
              )),
              if (_customModel)
                _cellField(TextField(
                  key: const Key('modelField'),
                  controller: _modelController,
                  autocorrect: false,
                  onSubmitted: (v) =>
                      settings.update(model: v.trim()),
                  onTapOutside: (_) => settings.update(
                      model: _modelController.text.trim()),
                  decoration: const InputDecoration(
                    labelText: 'Custom model name',
                    border: InputBorder.none,
                  ),
                )),
            ],
          ),
        GroupedSection(
          footer: 'The test names exactly what is broken: configuration, '
              'network, key, account credit, reply format, or quota.',
          children: [
            GroupedRow(
              key: const Key('testProviderButton'),
              icon: Icons.verified_outlined,
              iconColor: scheme.primary,
              title: 'Test This Provider',
              onTap: () => Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => DiagnosticsScreen(
                  diagnostics: ProviderDiagnostics(
                    settings: settings,
                    analyzer: widget.analyzer,
                  ),
                ),
              )),
            ),
            // Claude OAuth is meaningless when the server's payer is a
            // GLM/Doubao plan — and the sign-in it launches needs a VPN
            // for the mainland audience that picks those backends.
            if (_isServerProvider &&
                settings.serverBackend == 'claude' &&
                widget.startClaudeAuth != null)
              GroupedRow(
                key: const Key('connectClaudeButton'),
                icon: Icons.link,
                iconColor: scheme.tertiary,
                title: 'Connect Claude',
                trailing: _authBusy
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : null,
                onTap: _authBusy ? null : _connectClaude,
              ),
          ],
        ),
      ],
    );
  }
}
