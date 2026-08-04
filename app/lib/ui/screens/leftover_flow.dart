/// Leftover deduction flow (user feature 2026-08-04): photograph what you
/// DIDN'T finish, and the original meal's calories shrink to what you ate.
///
/// Two steps on one screen — pick the meal (today/yesterday), pick the
/// leftover photo — then the model estimates per-item remaining fractions
/// against the original analysis (core/leftover_logic.dart owns all math).
///
/// The WATCHER COLLISION is handled head-on: the leftover photo usually
/// lands in the camera roll, where the watcher may have already logged it
/// as a brand-new meal (the double-count this feature exists to kill).
/// If the photo's hash maps to a saved meal, the confirm step deletes
/// that duplicate in the same breath as applying the deduction; if the
/// watcher hasn't seen it yet, the hash is marked 'skipped' so it never
/// will.
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../../core/coerce.dart' show normalizeImageHash, safeNumber;
import '../../core/contracts.dart';
import '../../core/leftover_logic.dart';
import '../../services/photo/photo_hash.dart' show originalBytesMd5;
import '../format.dart';
import '../l10n.dart';
import '../services.dart';
import '../widgets/grouped.dart';

class LeftoverScreen extends StatefulWidget {
  const LeftoverScreen({super.key, required this.services});
  final UiServices services;

  @override
  State<LeftoverScreen> createState() => _LeftoverScreenState();
}

class _LeftoverScreenState extends State<LeftoverScreen> {
  List<Meal> _candidates = const [];
  Meal? _selected;
  bool _loading = true;
  bool _analyzing = false;
  bool _permissionDenied = false;
  Future<List<RecentAsset>>? _assets;
  final Map<String, Future<Uint8List?>> _thumbs = {};

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final now = DateTime.now();
    final today = isoDate(now);
    final yesterday = isoDate(now.subtract(const Duration(days: 1)));
    final meals = await widget.services.dao.mealsBetween(yesterday, today);
    if (!mounted) return;
    // Newest first: leftovers are almost always from the LAST meal.
    final food = byMealClock(meals.where(isFoodMeal)).reversed.toList();
    setState(() {
      _candidates = food;
      _loading = false;
    });
  }

  Future<List<RecentAsset>> _loadAssets() async {
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

  void _selectMeal(Meal m) {
    setState(() {
      _selected = m;
      _assets ??= _loadAssets();
    });
  }

  Future<void> _pickPhoto(RecentAsset asset) async {
    if (_analyzing) return;
    final meal = _selected;
    if (meal == null) return;
    setState(() => _analyzing = true);
    final services = widget.services;
    final l = context.l10n;
    try {
      final photo = await services.picker.loadOriginal(asset);
      if (!mounted) return;
      if (photo == null) {
        _snack(l.leftoverFailed);
        return;
      }
      final compact = compactOriginalAnalysis(meal.analysis);
      final reply =
          await services.analyzer.leftoverIntent(photo.bytes, compact);
      if (!mounted) return;
      if (reply == null) {
        _snack(l.leftoverFailed);
        return;
      }
      final hash = normalizeImageHash(originalBytesMd5(photo.bytes));
      final result = applyLeftover(meal.analysis, reply,
          leftoverPhotoMd5: hash,
          appliedAtIso: DateTime.now().toIso8601String());
      if (!mounted) return;
      if (result == null) {
        _snack(l.leftoverFailed);
        return;
      }
      // The estimation is done — the spinner must stop BEFORE any dialog
      // (an animating overlay behind a dialog never settles).
      setState(() => _analyzing = false);
      if (!result.sameMeal) {
        final useAnyway = await showDialog<bool>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: Text(ctx.l10n.leftoverNotSame),
            content: Text(result.note ?? ''),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(ctx, false),
                  child: Text(ctx.l10n.cancel)),
              FilledButton(
                  key: const Key('leftoverUseAnyway'),
                  onPressed: () => Navigator.pop(ctx, true),
                  child: Text(ctx.l10n.leftoverUseAnyway)),
            ],
          ),
        );
        if (useAnyway != true || !mounted) return;
      }
      await _confirmAndApply(meal, hash, result);
    } finally {
      if (mounted) setState(() => _analyzing = false);
    }
  }

  Future<void> _confirmAndApply(
      Meal meal, String hash, LeftoverResult result) async {
    final services = widget.services;
    // The watcher-collision check: did this leftover photo already become
    // a meal of its own?
    int? duplicateId;
    num duplicateKcal = 0;
    if (hash.isNotEmpty) {
      final status = await services.dao.photoStatus(hash);
      final dupId = status?.mealId;
      if (status?.status == IngestionStatus.saved &&
          dupId != null &&
          dupId != meal.id) {
        duplicateId = dupId;
        final rows = await services.dao
            .mealsBetween(meal.date, isoDate(DateTime.now()));
        for (final m in rows) {
          if (m.id == dupId) {
            duplicateKcal = safeCal(m);
            break;
          }
        }
      }
    }
    if (!mounted) return;

    final base = leftoverBase(meal.analysis);
    final baseKcal = safeNumber(base['total_calories']).round();
    final newKcal = safeNumber(result.adjusted['total_calories']).round();
    final pct = (result.eatenFraction * 100).round();
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(ctx.l10n.leftoverResultTitle),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(mealDescription(meal.analysis),
                style: Theme.of(ctx).textTheme.titleSmall),
            const SizedBox(height: 8),
            Text(
                ctx.l10n.leftoverResultLine(
                    '$pct', '${baseKcal - newKcal}', '$newKcal'),
                key: const Key('leftoverResultLine')),
            if (result.note != null && result.note!.isNotEmpty) ...[
              const SizedBox(height: 6),
              Text(result.note!,
                  style: Theme.of(ctx).textTheme.bodySmall?.copyWith(
                      color: Theme.of(ctx).colorScheme.onSurfaceVariant)),
            ],
            if (duplicateId != null) ...[
              const SizedBox(height: 8),
              Text(
                  ctx.l10n
                      .leftoverDupRemoved(formatKcal(duplicateKcal)),
                  key: const Key('leftoverDupLine'),
                  style: TextStyle(
                      color: Theme.of(ctx).colorScheme.tertiary)),
            ],
          ],
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: Text(ctx.l10n.cancel)),
          FilledButton(
              key: const Key('leftoverConfirm'),
              onPressed: () => Navigator.pop(ctx, true),
              child: Text(ctx.l10n.save)),
        ],
      ),
    );
    if (ok != true || !mounted) return;

    await services.dao.updateMealAnalysis(meal.id, result.adjusted);
    if (duplicateId != null) {
      // Removes the double-count AND tombstones the photo's ledger row so
      // the backfill scan can never resurrect it (deleteMeal contract).
      await services.dao.deleteMeal(duplicateId);
    } else if (hash.isNotEmpty) {
      // The watcher hasn't met this photo yet — make sure it never logs
      // it as food.
      await services.dao.markPhotoHash(hash, IngestionStatus.skipped);
    }
    if (!mounted) return;
    _snack(context.l10n.leftoverApplied);
    Navigator.of(context).pop(true);
  }

  void _snack(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(text)));
  }

  num safeCal(Meal m) => safeNumber(m.analysis['total_calories']);

  @override
  Widget build(BuildContext context) {
    final l = context.l10n;
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: Text(l.leftoverTitle)),
      body: Stack(children: [
        if (_loading)
          const Center(child: CircularProgressIndicator())
        else if (_selected == null)
          _mealPicker(theme, l)
        else
          _photoPicker(theme, l),
        if (_analyzing)
          Container(
            color: theme.colorScheme.surface.withValues(alpha: 0.7),
            child: Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const CircularProgressIndicator(),
                  const SizedBox(height: 12),
                  Text(l.analyzing),
                ],
              ),
            ),
          ),
      ]),
    );
  }

  Widget _mealPicker(ThemeData theme, dynamic l) {
    if (_candidates.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Text(l.leftoverNoMeals,
              key: const Key('leftoverNoMeals'),
              textAlign: TextAlign.center),
        ),
      );
    }
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        GroupedSection(
          header: l.leftoverPickMeal,
          children: [
            for (final m in _candidates)
              GroupedRow(
                key: Key('leftoverMeal-${m.id}'),
                title: mealDescription(m.analysis),
                value:
                    '${context.friendlyDay(m.date)} ${context.clock(m.time)}'
                    ' · ~${l.kcalAmount(formatKcal(safeCal(m)))}',
                onTap: () => _selectMeal(m),
              ),
          ],
        ),
      ],
    );
  }

  Widget _photoPicker(ThemeData theme, dynamic l) {
    final meal = _selected!;
    return Column(children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
        child: GroupedSection(
          children: [
            GroupedRow(
              key: const Key('leftoverChosenMeal'),
              title: mealDescription(meal.analysis),
              value: '~${l.kcalAmount(formatKcal(safeCal(meal)))}',
              trailing: TextButton(
                key: const Key('leftoverChangeMeal'),
                onPressed: () => setState(() => _selected = null),
                child: Text(l.leftoverChangeMeal),
              ),
              showChevron: false,
            ),
          ],
        ),
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(32, 12, 32, 4),
        child: Text(l.leftoverPickPhoto,
            style: theme.textTheme.titleSmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
      ),
      Expanded(
        child: FutureBuilder<List<RecentAsset>>(
          future: _assets,
          builder: (context, snap) {
            if (_permissionDenied) {
              return Center(child: Text(l.addNoPhotos));
            }
            if (!snap.hasData) {
              return const Center(child: CircularProgressIndicator());
            }
            final assets = snap.data!;
            if (assets.isEmpty) {
              return Center(child: Text(l.addNoPhotos));
            }
            return GridView.builder(
              padding: const EdgeInsets.all(16),
              gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                  crossAxisCount: 3, crossAxisSpacing: 6, mainAxisSpacing: 6),
              itemCount: assets.length,
              itemBuilder: (context, i) {
                final asset = assets[i];
                return InkWell(
                  key: Key('leftoverPhoto-${asset.id}'),
                  onTap: () => _pickPhoto(asset),
                  child: FutureBuilder<Uint8List?>(
                    future: _thumbFor(asset.id),
                    builder: (context, thumb) => thumb.data == null
                        ? Container(
                            color: theme.colorScheme.surfaceContainerHighest)
                        : Image.memory(thumb.data!,
                            fit: BoxFit.cover,
                            // A corrupt thumbnail must stay a tappable
                            // tile, never an error spray.
                            errorBuilder: (_, _, _) => Container(
                                color: theme
                                    .colorScheme.surfaceContainerHighest,
                                child: const Icon(Icons.image_outlined))),
                  ),
                );
              },
            );
          },
        ),
      ),
    ]);
  }
}
