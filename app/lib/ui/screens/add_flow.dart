/// Add flow (FAB): pick a recent photo (grid → analyzing spinner → meal
/// card, spec §2.3/§3/§6 pipeline with deliberate=true), or DESCRIBE a meal
/// in free text (NlExecutor.describeMeal → estimate → the meal editor as a
/// preview → insert), enter numbers manually, or FIX/delete a logged meal
/// (FixMealScreen → handleText, spec §4). Since 2026-07-31 this sheet is
/// the ONE owner of every meal action — the Today chat bar is gone.
library;

import 'package:flutter/foundation.dart' show compute;
import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../../services/analyzer/normalize.dart' show makeMealThumb;
import '../photo_pipeline.dart';
import 'fix_meal_screen.dart';
import 'meal_editor_screen.dart';
import '../services.dart';

/// Entry point wired to the FAB. [onChanged] refreshes the Today screen.
Future<void> openAddFlow(BuildContext context, UiServices services,
    {Future<void> Function()? onChanged}) async {
  final choice = await showModalBottomSheet<String>(
    context: context,
    builder: (ctx) => SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          ListTile(
            key: const Key('addFromPhotos'),
            leading: const Icon(Icons.photo_library_outlined),
            title: const Text('From recent photos'),
            onTap: () => Navigator.of(ctx).pop('photos'),
          ),
          ListTile(
            key: const Key('addFromText'),
            leading: const Icon(Icons.edit_note),
            title: const Text('Describe a meal'),
            subtitle: const Text('Say what you ate, in any language'),
            onTap: () => Navigator.of(ctx).pop('text'),
          ),
          // The zero-dependency path: the other two options need a working
          // AI key and spend a model call — with no key (first run), a
          // quota pause, or offline, the + button was a dead end.
          ListTile(
            key: const Key('addManually'),
            leading: const Icon(Icons.keyboard_alt_outlined),
            title: const Text('Enter manually'),
            subtitle: const Text('Type the numbers yourself — no AI'),
            onTap: () => Navigator.of(ctx).pop('manual'),
          ),
          // Corrections lived in a chat bar pinned to Today until
          // 2026-07-31 — the user's verdict: one button owns ALL meal
          // actions, adding and fixing alike.
          ListTile(
            key: const Key('addFixMeal'),
            leading: const Icon(Icons.build_outlined),
            title: const Text('Fix or delete a meal'),
            subtitle: const Text('"meal 2 was roast duck", "删除第一餐"'),
            onTap: () => Navigator.of(ctx).pop('fix'),
          ),
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
  late Future<List<IntakePhoto>> _photos;
  bool _analyzing = false;
  bool _permissionDenied = false;

  @override
  void initState() {
    super.initState();
    _photos = _load();
  }

  /// Ask for permission FIRST: pickFromRecent returns [] on denial, which
  /// rendered as 'No recent photos found.' — indistinguishable from an
  /// empty camera roll and, once the OS stops re-prompting, a permanent
  /// dead end in the app's headline flow.
  Future<List<IntakePhoto>> _load() async {
    final granted = await widget.services.requestPhotoPermission();
    if (!mounted) return const [];
    if (!granted) {
      setState(() => _permissionDenied = true);
      return const [];
    }
    return widget.services.picker.recentPhotos();
  }

  Future<void> _analyze(IntakePhoto photo) async {
    setState(() => _analyzing = true);
    // User-picked → deliberate: reclaims failed/skipped/deleted ledger rows
    // (spec §2.3 caller policies).
    final deliberate = IntakePhoto(photo.bytes, photo.assetId, photo.fileName,
        capturedAt: photo.capturedAt, deliberate: true);
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
          FutureBuilder<List<IntakePhoto>>(
            future: _photos,
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
              final photos = snap.data ?? const [];
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
              if (photos.isEmpty) {
                return const Center(child: Text('No recent photos found.'));
              }
              return GridView.builder(
                padding: const EdgeInsets.all(8),
                gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                    crossAxisCount: 3, crossAxisSpacing: 4, mainAxisSpacing: 4),
                itemCount: photos.length,
                itemBuilder: (context, i) => GestureDetector(
                  key: Key('recentPhoto$i'),
                  onTap: _analyzing ? null : () => _analyze(photos[i]),
                  child: Image.memory(photos[i].bytes,
                      fit: BoxFit.cover, gaplessPlayback: true),
                ),
              );
            },
          ),
          if (_analyzing)
            Container(
              color: Colors.black45,
              child: const Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    CircularProgressIndicator(key: Key('photoAnalyzing')),
                    SizedBox(height: 12),
                    Text('Analyzing…',
                        style: TextStyle(color: Colors.white)),
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

