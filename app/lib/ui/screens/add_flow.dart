/// Add flow (FAB): pick a recent photo (grid → analyzing spinner → meal
/// card, spec §2.3/§3/§6 pipeline with deliberate=true), or DESCRIBE a meal
/// in free text (NlExecutor.describeMeal → estimate → the meal editor as a
/// preview → insert), enter numbers manually, or FIX/delete a logged meal
/// (FixMealScreen → handleText, spec §4). Since 2026-07-31 this sheet is
/// the ONE owner of every meal action — the Today chat bar is gone.
library;

import 'dart:typed_data';

import 'package:flutter/foundation.dart' show compute;
import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../../services/analyzer/normalize.dart' show makeMealThumb;
import '../photo_pipeline.dart';
import '../l10n.dart';
import '../widgets/grouped.dart';
import 'fix_meal_screen.dart';
import 'meal_editor_screen.dart';
import '../services.dart';

/// Entry point wired to the FAB. [onChanged] refreshes the Today screen.
Future<void> openAddFlow(BuildContext context, UiServices services,
    {Future<void> Function()? onChanged}) async {
  // Native-ized in loop cycle 2 (interaction lens #1): drag handle, a
  // title, and the same grouped-row language as Settings — the sheet is
  // the app's single logging entry point and looked stock-Material.
  final choice = await showModalBottomSheet<String>(
    context: context,
    showDragHandle: true,
    useSafeArea: true,
    backgroundColor: groupedBackground(Theme.of(context).colorScheme),
    builder: (ctx) => SafeArea(
      top: false,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 4),
            child: Align(
              alignment: Alignment.centerLeft,
              child: Text(context.l10n.addSheetTitle,
                  style: Theme.of(ctx)
                      .textTheme
                      .titleLarge
                      ?.copyWith(fontWeight: FontWeight.w600)),
            ),
          ),
          GroupedSection(
            children: [
              GroupedRow(
                key: const Key('addFromPhotos'),
                icon: Icons.photo_library_outlined,
                iconColor: Theme.of(ctx).colorScheme.primary,
                title: ctx.l10n.addFromPhotos,
                onTap: () => Navigator.of(ctx).pop('photos'),
              ),
              GroupedRow(
                key: const Key('addFromText'),
                icon: Icons.edit_note,
                iconColor: Theme.of(ctx).colorScheme.secondary,
                title: ctx.l10n.addDescribe,
                value: ctx.l10n.addDescribeNote,
                onTap: () => Navigator.of(ctx).pop('text'),
              ),
              // The zero-dependency path: the other two options need a
              // working AI key and spend a model call — with no key
              // (first run), a quota pause, or offline, the + button was
              // a dead end.
              GroupedRow(
                key: const Key('addManually'),
                icon: Icons.keyboard_alt_outlined,
                iconColor: Theme.of(ctx).colorScheme.tertiary,
                title: ctx.l10n.addManual,
                value: ctx.l10n.addManualNote,
                onTap: () => Navigator.of(ctx).pop('manual'),
              ),
            ],
          ),
          // Corrections lived in a chat bar pinned to Today until
          // 2026-07-31 — the user's verdict: one button owns ALL meal
          // actions, adding and fixing alike.
          GroupedSection(
            footer: ctx.l10n.addFixFooter,
            children: [
              GroupedRow(
                key: const Key('addFixMeal'),
                icon: Icons.build_outlined,
                iconColor: Theme.of(ctx).colorScheme.error,
                title: ctx.l10n.addFix,
                onTap: () => Navigator.of(ctx).pop('fix'),
              ),
            ],
          ),
          const SizedBox(height: 16),
        ],
      ),
    ),
  );
  if (!context.mounted) return;
  if (choice == 'photos') {
    await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => AddPhotoScreen(services: services)));
  } else if (choice == 'text') {
    await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => AddTextScreen(
            executor: services.executor, dao: services.dao)));
  } else if (choice == 'manual') {
    // Defaults to today/now inside the editor (MealDraft.blank).
    await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => MealEditorScreen(dao: services.dao)));
  } else if (choice == 'fix') {
    await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => FixMealScreen(executor: services.executor)));
  } else {
    return;
  }
  await onChanged?.call();
}

/// Recent-photos grid → tap → analyzing spinner → result card.
class AddPhotoScreen extends StatefulWidget {
  final UiServices services;
  const AddPhotoScreen({super.key, required this.services});

  @override
  State<AddPhotoScreen> createState() => _AddPhotoScreenState();
}

class _AddPhotoScreenState extends State<AddPhotoScreen> {
  late Future<List<RecentAsset>> _assets;
  bool _analyzing = false;
  bool _permissionDenied = false;
  // Future-cached per asset: dedupes in-flight fetches across rebuilds
  // (the same rule the coverage screen learned).
  final Map<String, Future<Uint8List?>> _thumbs = {};

  @override
  void initState() {
    super.initState();
    _assets = _load();
  }

  /// Ask for permission FIRST: the picker returns [] on denial, which
  /// rendered as 'No recent photos found.' — indistinguishable from an
  /// empty camera roll and, once the OS stops re-prompting, a permanent
  /// dead end in the app's headline flow.
  ///
  /// LISTS assets only. The previous version read up to 30 ORIGINALS (25 MB
  /// each) before showing anything and then decoded each 12 MP image
  /// full-res into a 120 px cell — hundreds of MB resident, on a screen
  /// that uses exactly one photo.
  Future<List<RecentAsset>> _load() async {
    final granted = await widget.services.requestPhotoPermission();
    if (!mounted) return const [];
    if (!granted) {
      setState(() => _permissionDenied = true);
      return const [];
    }
    return widget.services.picker.recentAssets();
  }

  Future<Uint8List?> _thumbFor(String assetId) => _thumbs.putIfAbsent(
      assetId, () => widget.services.picker.thumbnail(assetId));

  Future<void> _pick(RecentAsset asset) async {
    // Guard BEFORE the async gap: onTap reads _analyzing from the last
    // BUILD, so two taps in the same frame both pass that check and run
    // two analyses — two model calls, two ledger reservations, for one
    // meal (review 2026-07-31).
    if (_analyzing) return;
    setState(() => _analyzing = true);
    // ORIGINAL bytes for THIS photo only — md5 identity (§6.2) needs the
    // original, but only the chosen one.
    final deliberate = await widget.services.picker.loadOriginal(asset);
    if (!mounted) return;
    if (deliberate == null) {
      setState(() => _analyzing = false);
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('That photo could not be read (too large or '
              'removed).')));
      return;
    }
    await _analyze(deliberate);
  }

  Future<void> _analyze(IntakePhoto deliberate) async {
    if (!_analyzing) setState(() => _analyzing = true);
    // Through the APP'S ONE pipeline (services.processPhoto → enqueue), not
    // a private instance: a second analyzer call concurrent with the
    // watcher self-inflicts rate limits and doubles resident photo bytes.
    final process = widget.services.processPhoto;
    final outcome = process != null
        ? await process(deliberate)
        : await PhotoPipeline(
                dao: widget.services.dao, analyzer: widget.services.analyzer)
            .process(deliberate);
    if (!mounted) return;
    setState(() => _analyzing = false);
    // A refused photo must not be a dead end: the model saying "not food"
    // (routine for drinks) or failing permanently would otherwise leave the
    // user with no way to log that photo AT ALL — re-picking it just
    // repeats the same verdict.
    final canLogManually = outcome.kind == PhotoOutcomeKind.skipped ||
        outcome.kind == PhotoOutcomeKind.failed;
    final choice = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(switch (outcome.kind) {
          PhotoOutcomeKind.saved => 'Meal logged',
          PhotoOutcomeKind.skipped => 'No food detected',
          PhotoOutcomeKind.duplicate => 'Duplicate photo',
          PhotoOutcomeKind.alreadyTracked => 'Already logged',
          PhotoOutcomeKind.failed => 'Analysis failed',
        }),
        content: Text(canLogManually
            ? '${outcome.message}\n\nIf this IS food, log it yourself — '
                'the photo stays attached to the meal.'
            : outcome.message),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop('ok'),
            child: const Text('OK'),
          ),
          if (canLogManually)
            FilledButton(
              key: const Key('logManuallyButton'),
              onPressed: () => Navigator.of(ctx).pop('manual'),
              child: const Text('Log manually'),
            ),
        ],
      ),
    );
    if (!mounted) return;
    if (choice == 'manual') {
      final saved = await Navigator.of(context).push<bool>(MaterialPageRoute(
        builder: (_) => MealEditorScreen(
          dao: widget.services.dao,
          fromPhoto: deliberate,
          makeThumb: (bytes) => compute(makeMealThumb, bytes),
        ),
      ));
      if (saved == true && mounted) {
        Navigator.of(context).pop();
        return;
      }
    }
    if (outcome.kind == PhotoOutcomeKind.saved && mounted) {
      Navigator.of(context).pop();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Recent photos')),
      body: Stack(
        children: [
          FutureBuilder<List<RecentAsset>>(
            future: _assets,
            builder: (context, snap) {
              if (snap.connectionState != ConnectionState.done) {
                return const Center(child: CircularProgressIndicator());
              }
              if (snap.hasError) {
                return Center(
                    child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Text('Could not load photos: ${snap.error}'),
                ));
              }
              final assets = snap.data ?? const <RecentAsset>[];
              if (_permissionDenied) {
                final open = widget.services.openSystemSettings;
                return Center(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        const Text(
                          "CalorieTracker isn't allowed to see your "
                          'photos.',
                          key: Key('photoPermissionDenied'),
                          textAlign: TextAlign.center,
                        ),
                        if (open != null) ...[
                          const SizedBox(height: 12),
                          FilledButton.tonal(
                            key: const Key('openSystemSettings'),
                            onPressed: () => open(),
                            child: const Text('Open system settings'),
                          ),
                        ],
                      ],
                    ),
                  ),
                );
              }
              if (assets.isEmpty) {
                return Center(
                    child: Text(context.l10n.addNoPhotos));
              }
              return Column(children: [
                // Portion accuracy is the model's weakest link, and a scale
                // reference in frame is the cheapest fix the USER controls.
                Padding(
                  padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
                  child: Row(
                    key: const Key('scaleReferenceTip'),
                    children: [
                      Icon(Icons.straighten,
                          size: 16,
                          color:
                              Theme.of(context).colorScheme.onSurfaceVariant),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          context.l10n.addPhotosTip,
                          style: Theme.of(context)
                              .textTheme
                              .bodySmall
                              ?.copyWith(
                                  color: Theme.of(context)
                                      .colorScheme
                                      .onSurfaceVariant),
                        ),
                      ),
                    ],
                  ),
                ),
                Expanded(
                    child: GridView.builder(
                padding: const EdgeInsets.all(8),
                gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                    crossAxisCount: 3, crossAxisSpacing: 4, mainAxisSpacing: 4),
                itemCount: assets.length,
                itemBuilder: (context, i) => GestureDetector(
                  key: Key('recentPhoto$i'),
                  onTap: _analyzing ? null : () => _pick(assets[i]),
                  child: FutureBuilder<Uint8List?>(
                    future: _thumbFor(assets[i].id),
                    builder: (context, snap) {
                      final bytes = snap.data;
                      if (bytes == null || bytes.isEmpty) {
                        return Container(
                            color: Theme.of(context)
                                .colorScheme
                                .surfaceContainerHighest);
                      }
                      // cacheWidth: decode at cell size, not 12 MP.
                      return Image.memory(bytes,
                          fit: BoxFit.cover,
                          cacheWidth: 320,
                          gaplessPlayback: true);
                    },
                  ),
                ),
              )),
              ]);
            },
          ),
          if (_analyzing)
            Container(
              color: Colors.black45,
              child: Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const CircularProgressIndicator(
                        key: Key('photoAnalyzing')),
                    const SizedBox(height: 12),
                    Text(context.l10n.analyzing,
                        style: const TextStyle(color: Colors.white)),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// Describe-a-meal entry: free text (any language) → describeMeal's
/// new-meal-only estimate → the meal editor as a PREVIEW → insert with
/// source 'manual_text' (spec §4.6 parity). Nothing is written until the
/// user saves from the editor, so a model guess about food it never saw
/// cannot silently enter the day's totals.
class AddTextScreen extends StatefulWidget {
  final NlExecutor executor;
  final MealsDao dao;
  const AddTextScreen(
      {super.key, required this.executor, required this.dao});

  @override
  State<AddTextScreen> createState() => _AddTextScreenState();
}

class _AddTextScreenState extends State<AddTextScreen> {
  final TextEditingController _controller = TextEditingController();
  bool _sending = false;
  String? _error;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  /// Describe → PREVIEW → save. The analysis opens in the meal editor
  /// instead of being written straight to the log: a text estimate is the
  /// model's guess about a meal it never saw, so the numbers deserve a look
  /// before they enter the day's totals (and the editor already owns all the
  /// validation).
  Future<void> _describe() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _sending) return;
    setState(() {
      _sending = true;
      _error = null;
    });
    final outcome = await widget.executor.describeMeal(text);
    if (!mounted) return;
    setState(() => _sending = false);
    if (!outcome.ok) {
      setState(() => _error = outcome.error);
      return;
    }
    final warning = outcome.warning;
    if (warning != null) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(warning)));
    }
    final saved = await Navigator.of(context).push<bool>(MaterialPageRoute(
      builder: (_) => MealEditorScreen(
        dao: widget.dao,
        initialAnalysis: outcome.analysis,
        newMealSource: MealSource.manualText, // spec §4.6 parity
      ),
    ));
    if (saved == true && mounted) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Describe a meal')),
      // SCROLLABLE: the field is autofocused (keyboard up on entry) and the
      // inline messages can run several lines — the quota-pause refusal is
      // four paragraphs. In a fixed Column that pushed the primary button
      // off-screen, where it is not even hit-testable (measured at 390x844
      // and smaller by the review probes).
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            TextField(
              key: const Key('addTextField'),
              controller: _controller,
              maxLines: 4,
              autofocus: true,
              textInputAction: TextInputAction.newline,
              decoration: const InputDecoration(
                labelText: 'What did you eat?',
                hintText: 'e.g. "two eggs and toast with butter"\n'
                    'or "一碗牛肉面加一个鸡蛋"',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            Text(
              'Any language works. You will see the estimate and can fix it '
              'before it is saved.',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
            if (_error != null) ...[
              const SizedBox(height: 12),
              Text(_error!,
                  key: const Key('addTextError'),
                  style: TextStyle(color: theme.colorScheme.error)),
            ],
            const SizedBox(height: 12),
            FilledButton.icon(
              key: const Key('addTextSend'),
              onPressed: _sending ? null : _describe,
              icon: _sending
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.auto_awesome),
              label: Text(_sending ? 'Estimating…' : 'Estimate this meal'),
            ),
          ],
        ),
      ),
    );
  }
}

