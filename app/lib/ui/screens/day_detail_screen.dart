/// One day of history: totals with a macro breakdown, every meal on the day
/// (tap to edit, long-press-free — editing is an explicit screen), and a FAB
/// to add a meal by hand to THIS date.
library;

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../format.dart';
import '../widgets/macro_chart.dart';
import 'meal_editor_screen.dart';

class DayDetailScreen extends StatefulWidget {
  const DayDetailScreen({
    super.key,
    required this.dao,
    required this.date,
    this.now,
  });

  final MealsDao dao;
  final String date; // YYYY-MM-DD
  final DateTime Function()? now;

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
      final meals = await widget.dao.mealsBetween(widget.date, widget.date);
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
    if (saved == true) await _load();
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
                            meal: m,
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
  const _MealRow({required this.meal, required this.onTap});

  final Meal meal;
  final VoidCallback onTap;

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
