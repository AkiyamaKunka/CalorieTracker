/// One day of history: totals with a macro breakdown, every meal on the day
/// (tap to edit, long-press-free — editing is an explicit screen), and a FAB
/// to add a meal by hand to THIS date.
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../format.dart';
import '../meal_thumbs.dart';
import '../widgets/macro_chart.dart';
import '../widgets/meal_thumb.dart';
import 'meal_editor_screen.dart';

class DayDetailScreen extends StatefulWidget {
  const DayDetailScreen({
    super.key,
    required this.dao,
    required this.date,
    this.now,
    this.thumbs,
  });

  final MealsDao dao;
  final String date; // YYYY-MM-DD
  final DateTime Function()? now;

  /// Photo thumbnails for the rows; null (tests) renders placeholders.
  final MealThumbResolver? thumbs;

  @override
  State<DayDetailScreen> createState() => _DayDetailScreenState();
}

class _DayDetailScreenState extends State<DayDetailScreen> {
  bool _loading = true;
  String? _error;
  List<Meal> _meals = const [];

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      // Ordered by the meal's own clock: the DAO sorts by ingestion
      // timestamp, which puts a hand-added or re-dated meal last.
      final meals =
          byMealClock(await widget.dao.mealsBetween(widget.date, widget.date));
      if (!mounted) return;
      setState(() {
        _meals = meals;
        _loading = false;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = 'Could not load this day: $e';
      });
    }
  }

  Future<void> _openEditor({Meal? meal}) async {
    final saved = await Navigator.of(context).push<bool>(MaterialPageRoute(
      builder: (_) => MealEditorScreen(
        dao: widget.dao,
        meal: meal,
        initialDate: widget.date,
        now: widget.now,
      ),
    ));
    if (saved == true) {
      // The editor may have deleted the meal (SQLite can reuse a rowid
      // later) or re-dated it; drop any cached thumb for this id.
      if (meal != null) widget.thumbs?.evict(meal.id);
      await _load();
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final totals = todayTotals(_meals);
    // The caller reloads on ANY return (cheap local query) — no result
    // plumbing, so a system back-gesture behaves like the back button.
    return PopScope(
      canPop: true,
      onPopInvokedWithResult: (didPop, _) {},
      child: Scaffold(
        appBar: AppBar(
          title: Text(friendlyHistoryDay(widget.date,
              now: (widget.now ?? DateTime.now)())),
        ),
        body: _loading
            ? const Center(child: CircularProgressIndicator())
            : _error != null
                ? Center(
                    child: Padding(
                      padding: const EdgeInsets.all(24),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(_error!, textAlign: TextAlign.center),
                          const SizedBox(height: 12),
                          FilledButton(
                              onPressed: _load, child: const Text('Retry')),
                        ],
                      ),
                    ),
                  )
                : RefreshIndicator(
                    onRefresh: _load,
                    child: ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      padding: const EdgeInsets.fromLTRB(16, 12, 16, 96),
                      children: [
                        Text('~${formatKcal(totals.cal)} kcal',
                            key: const Key('dayTotalKcal'),
                            style: theme.textTheme.headlineSmall),
                        Text(
                            '${totals.meals} '
                            '${totals.meals == 1 ? 'meal' : 'meals'}',
                            style: theme.textTheme.bodySmall?.copyWith(
                                color: theme.colorScheme.onSurfaceVariant)),
                        const SizedBox(height: 12),
                        MacroBreakdownBar(
                          proteinG: totals.protein,
                          carbsG: totals.carbs,
                          fatG: totals.fat,
                        ),
                        const SizedBox(height: 8),
                        const Divider(height: 32),
                        if (_meals.isEmpty)
                          Padding(
                            padding: const EdgeInsets.symmetric(vertical: 24),
                            child: Text(
                              'Nothing logged on this day yet. '
                              'Tap + to add a meal.',
                              key: const Key('dayEmpty'),
                              textAlign: TextAlign.center,
                              style: theme.textTheme.bodyMedium?.copyWith(
                                  color: theme.colorScheme.onSurfaceVariant),
                            ),
                          ),
                        for (final m in _meals)
                          _MealRow(
                            // Identity key: element state must follow the
                            // MEAL when the list shifts (a delete above
                            // otherwise leaves this row showing its old
                            // neighbour's resolving photo).
                            key: ValueKey('meal${m.id}'),
                            meal: m,
                            thumb: widget.thumbs?.thumbFor(m),
                            onTap: () => _openEditor(meal: m),
                          ),
                      ],
                    ),
                  ),
        floatingActionButton: FloatingActionButton(
          key: const Key('addMealToDay'),
          onPressed: () => _openEditor(),
          child: const Icon(Icons.add),
        ),
      ),
    );
  }
}

class _MealRow extends StatelessWidget {
  const _MealRow(
      {super.key, required this.meal, required this.onTap, this.thumb});

  final Meal meal;
  final VoidCallback onTap;
  final Future<Uint8List?>? thumb;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final a = meal.analysis;
    final food = isFoodMeal(meal);
    return Card(
      margin: const EdgeInsets.symmetric(vertical: 6),
      child: InkWell(
        key: Key('mealRow${meal.id}'),
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  MealThumbView(
                      thumb: thumb, isPhotoMeal: meal.imageHash.isNotEmpty),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(mealDescription(a),
                        style: theme.textTheme.titleSmall),
                  ),
                  const SizedBox(width: 8),
                  Text(meal.time,
                      style: theme.textTheme.bodySmall?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant)),
                ],
              ),
              const SizedBox(height: 6),
              Row(
                children: [
                  Text('~${displayTotalCalories(a)} kcal',
                      style: theme.textTheme.bodyMedium),
                  if (meal.corrected) ...[
                    const SizedBox(width: 8),
                    Icon(Icons.edit_outlined,
                        size: 13, color: theme.colorScheme.onSurfaceVariant),
                  ],
                  if (!food) ...[
                    const SizedBox(width: 8),
                    Text('not food',
                        style: theme.textTheme.bodySmall?.copyWith(
                            color: theme.colorScheme.onSurfaceVariant)),
                  ],
                ],
              ),
              if (food) ...[
                const SizedBox(height: 10),
                MacroBreakdownBar(
                  proteinG: safeMacro(a, 'total_protein_g'),
                  carbsG: safeMacro(a, 'total_carbs_g'),
                  fatG: safeMacro(a, 'total_fat_g'),
                  height: 12,
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
