/// The API-key connection type (user-designed IA, 2026-08-02 v2: each
/// connection type gets its OWN page). Six vendors as a checkmark list;
/// the selected vendor's key + model configuration appears below it.
/// Pay-per-photo, key on this phone.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show HapticFeedback;

import '../../services.dart' show SettingsStore;
import '../../l10n.dart';
import '../../widgets/grouped.dart';
import 'provider_page.dart'
    show kApiKeyChoices, kCustomModelSentinel, kKnownModels, isCuratedModel,
        providerLabel;

class ApiKeyProviderPage extends StatefulWidget {
  const ApiKeyProviderPage({super.key, required this.settings});
  final SettingsStore settings;

  @override
  State<ApiKeyProviderPage> createState() => _ApiKeyProviderPageState();
}

class _ApiKeyProviderPageState extends State<ApiKeyProviderPage> {
  late final TextEditingController _keyController;
  late final TextEditingController _modelController;
  late bool _customModel;

  bool get _apiActive => widget.settings.provider != 'server';

  @override
  void initState() {
    super.initState();
    _keyController = TextEditingController(
        text: _apiActive ? widget.settings.apiKey : '');
    _modelController = TextEditingController(text: widget.settings.model);
    _customModel =
        !isCuratedModel(widget.settings.provider, widget.settings.model);
  }

  @override
  void dispose() {
    _keyController.dispose();
    _modelController.dispose();
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

  /// Where the key comes from — the footer's job.
  String _keyFooter(BuildContext context) =>
      switch (widget.settings.provider) {
        'qwen' => context.l10n.apiKeyFooterQwen,
        'doubao' => context.l10n.apiKeyFooterDoubao,
        'glm' => context.l10n.apiKeyFooterGlm,
        _ => context.l10n.apiKeyFooterDefault,
      };

  Widget _cellField(Widget field) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
        child: field,
      );

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final settings = widget.settings;
    return GroupedPage(
      title: context.l10n.apiPageTitle,
      children: [
        GroupedSection(
          header: context.l10n.apiProviderHeader,
          footer: context.l10n.apiProviderFooter,
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
        if (_apiActive) ...[
          GroupedSection(
            header: context.l10n.apiKeyHeader,
            footer: _keyFooter(context),
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
                  labelText: context.l10n
                      .apiKeyLabel(providerLabel(settings.provider)),
                  border: InputBorder.none,
                ),
              )),
            ],
          ),
          GroupedSection(
            header: context.l10n.modelHeader,
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
                    DropdownMenuItem(
                        value: kCustomModelSentinel,
                        child: Text(context.l10n.modelCustomRow)),
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
                  onSubmitted: (v) => settings.update(model: v.trim()),
                  onTapOutside: (_) => settings.update(
                      model: _modelController.text.trim()),
                  decoration: InputDecoration(
                    labelText: context.l10n.modelCustomLabel,
                    border: InputBorder.none,
                  ),
                )),
            ],
          ),
        ] else
          GroupedSection(
            footer: context.l10n.apiInactiveFooter,
            children: const [SizedBox(height: 1)],
          ),
      ],
    );
  }
}
