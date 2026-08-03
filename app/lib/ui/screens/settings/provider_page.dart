/// The AI Provider chooser (user-designed IA, 2026-08-02 v2): TWO
/// connection types, each on its OWN page —
///
///   API Key · pay per photo        → ApiKeyProviderPage
///   Subscription · via your server → SubscriptionProviderPage
///
/// This page shows which type is active (checkmark + the concrete choice
/// as the row value) and hosts the one Test action; everything else lives
/// one level down. Shared constants for both pages live here.
library;

import 'package:flutter/material.dart';

import '../../../core/contracts.dart';
import '../../diagnostics.dart';
import '../../services.dart' show SettingsStore;
import '../../l10n.dart';
import '../../widgets/grouped.dart';
import '../diagnostics_screen.dart';
import 'api_key_page.dart';
import 'subscription_page.dart';

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

/// Short display name for an API provider id.
String providerLabel(String provider) => switch (provider) {
      'openai' => 'OpenAI',
      'anthropic' => 'Claude',
      'server' => 'My server',
      'qwen' => 'Qwen 通义千问',
      'doubao' => 'Doubao 豆包',
      'glm' => 'GLM 智谱',
      _ => 'Gemini',
    };

/// The two CONNECTION TYPES: an API key is pay-per-photo with the key on
/// this phone; an Agent/Coding plan is a flat-rate subscription the
/// user's own server signs into. The note carries the one deciding fact.
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
  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final settings = widget.settings;
    final planActive = settings.provider == 'server';
    return GroupedPage(
      title: context.l10n.providerPageTitle,
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
          header: context.l10n.connectionTypeHeader,
          footer: context.l10n.connectionTypeFooter,
          children: [
            GroupedRow(
              key: const Key('apiKeyTypeRow'),
              icon: Icons.vpn_key_outlined,
              iconColor: scheme.primary,
              title: context.l10n.typeApiKey,
              value: planActive
                  ? null
                  : providerLabel(settings.provider),
              trailing: planActive
                  ? null
                  : Icon(Icons.check, size: 20, color: scheme.primary),
              showChevron: true,
              onTap: () async {
                await Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) =>
                      ApiKeyProviderPage(settings: settings),
                ));
                if (mounted) setState(() {});
              },
            ),
            GroupedRow(
              key: const Key('subscriptionTypeRow'),
              icon: Icons.workspace_premium_outlined,
              iconColor: scheme.tertiary,
              title: context.l10n.typeSubscription,
              value: planActive
                  ? providerDisplayLabel('server', settings.serverBackend)
                  : null,
              trailing: planActive
                  ? Icon(Icons.check, size: 20, color: scheme.primary)
                  : null,
              showChevron: true,
              onTap: () async {
                await Navigator.of(context).push(MaterialPageRoute(
                  builder: (_) => SubscriptionProviderPage(
                    settings: settings,
                    startClaudeAuth: widget.startClaudeAuth,
                    completeClaudeAuth: widget.completeClaudeAuth,
                    openUrl: widget.openUrl,
                  ),
                ));
                if (mounted) setState(() {});
              },
            ),
          ],
        ),
        GroupedSection(
          footer: context.l10n.testProviderFooter,
          children: [
            GroupedRow(
              key: const Key('testProviderButton'),
              icon: Icons.verified_outlined,
              iconColor: scheme.primary,
              title: context.l10n.testProvider,
              onTap: () => Navigator.of(context).push(MaterialPageRoute(
                builder: (_) => DiagnosticsScreen(
                  diagnostics: ProviderDiagnostics(
                    settings: settings,
                    analyzer: widget.analyzer,
                  ),
                ),
              )),
            ),
          ],
        ),
      ],
    );
  }
}
