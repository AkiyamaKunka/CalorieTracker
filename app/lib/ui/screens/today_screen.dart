/// Today: running totals header (spec §5.1), typical-day line, today's meal
/// cards, pull-to-refresh, and the one Meals button (add + fix, spec §4
/// corrections now live in its sheet — the pinned chat bar is gone,
/// 2026-07-31).
library;

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../format.dart';
import '../meal_thumbs.dart';
import '../widgets/meal_card.dart';
import 'meal_editor_screen.dart';

class TodayScreen extends StatefulWidget {
  final MealsDao dao;
  final NlExecutor executor;

  /// Photo thumbnails for the cards; null (tests) renders placeholders.
  final MealThumbResolver? thumbs;

  /// Opens the meals sheet (add / describe / manual / fix). Null (tests /
  /// other hosts) hides the button.
  final VoidCallback? onAdd;
  const TodayScreen(
      {super.key,
      required this.dao,
      required this.executor,
      this.thumbs,
      this.onAdd});

  @override
  State<TodayScreen> createState() => TodayScreenState();
}

class TodayScreenState extends State<TodayScreen> {
  bool _loading = true;
  String? _error;
  List<Meal> _todayMeals = const [];
  Map<String, num> _priorDayTotals = const {};

  @override
  void initState() {
    super.initState();
    reload();
  }

  Future<void> reload() async {
    final now = DateTime.now();
    final today = isoDate(now);
    try {
      final todayMeals =
          byMealClock(await widget.dao.mealsBetween(today, today));
      // Typical-day window: prior 7 local days EXCLUDING today (spec §5.1).
      final prior = await widget.dao.mealsBetween(
        isoDate(now.subtract(const Duration(days: 7))),
        isoDate(now.subtract(const Duration(days: 1))),
      );
      if (!mounted) return;
      setState(() {
        _todayMeals = todayMeals;
        _priorDayTotals = dailyCalorieTotals(prior);
        _loading = false;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = 'Could not load today\'s meals: $e';
      });
    }
  }

  /// Tap-through to the editor: correcting a number by hand is often faster
  /// than typing a sentence, and it costs no model call.
  Future<void> _editMeal(Meal meal) async {
    await Navigator.of(context).push(MaterialPageRoute(
      builder: (_) => MealEditorScreen(dao: widget.dao, meal: meal),
    ));
    widget.thumbs?.evict(meal.id); // deleted/re-dated: cache must not lie
    if (mounted) await reload();
  }

  @override
  Widget build(BuildContext context) {
    final onAdd = widget.onAdd;
    // The chat bar that used to sit under this Stack is GONE
    // (2026-07-31, user request): corrections live in the meals button's
    // sheet ("Fix or delete a meal"), so Today is purely the day's list
    // plus the one button.
    return Stack(
      children: [
        _body(context),
        if (onAdd != null)
          Positioned(
            right: 16,
            bottom: 16,
            child: FloatingActionButton.extended(
              key: const Key('addMealFab'),
              onPressed: onAdd,
              icon: const Icon(Icons.add),
              label: const Text('Meals'),
            ),
          ),
      ],
    );
  }

  Widget _body(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(_error!, textAlign: TextAlign.center),
              const SizedBox(height: 12),
              FilledButton(onPressed: reload, child: const Text('Retry')),
            ],
          ),
        ),
      );
    }
    final foodMeals = _todayMeals.where(isFoodMeal).toList();
    return RefreshIndicator(
      onRefresh: reload,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.only(bottom: 96),
        children: [
          _totalsHeader(context, foodMeals),
          if (foodMeals.isEmpty)
            Padding(
              padding: const EdgeInsets.all(32),
              child: Center(
                child: Column(
                  children: [
                    // Empty copy per spec §5.1; the hint below is app-only
                    // (§9) — the bare sentence was a dead-ish end for a
                    // user who just finished setup.
                    const Text('No meals logged yet today.'),
                    const SizedBox(height: 8),
                    Text(
                      'Tap "Meals" below to log one from a photo or a '
                      'description — or turn on "Watch camera roll" in '
                      'Settings and new food photos log themselves.',
                      textAlign: TextAlign.center,
                      style: Theme.of(context).textTheme.bodySmall?.copyWith(
                          color:
                              Theme.of(context).colorScheme.onSurfaceVariant),
                    ),
                  ],
                ),
              ),
            )
          else
            for (final meal in foodMeals)
              MealCard(
                key: ValueKey('meal${meal.id}'),
                meal: meal,
                onTap: () => _editMeal(meal),
                thumb: widget.thumbs?.thumbFor(meal),
              ),
        ],
      ),
    );
  }

  Widget _totalsHeader(BuildContext context, List<Meal> foodMeals) {
    final theme = Theme.of(context);
    final totals = todayTotals(foodMeals); // Σ safeNumber, spec §5.1
    final typical = typicalDayKcal(_priorDayTotals);
    String? typicalLine;
    if (typical != null) {
      final total = totals.cal.round();
      // Headroom vs above-typical wording, spec §5.1.
      typicalLine = total <= typical
          ? 'Typical day: ~${formatKcal(typical)} kcal · '
              '~${formatKcal(typical - total)} kcal headroom'
          : 'Typical day: ~${formatKcal(typical)} kcal · '
              '~${formatKcal(total - typical)} kcal above typical';
    }
    return Card(
      margin: const EdgeInsets.all(12),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('${formatKcal(totals.cal)} kcal',
                key: const Key('todayTotalKcal'),
                style: theme.textTheme.headlineMedium),
            const SizedBox(height: 4),
            Text(
              'Protein: ${totals.protein}g · Carbs: ${totals.carbs}g · '
              'Fat: ${totals.fat}g · Meals: ${totals.meals}',
              style: theme.textTheme.bodyMedium,
            ),
            if (typicalLine != null) ...[
              const SizedBox(height: 6),
              Text(typicalLine,
                  key: const Key('typicalDayLine'),
                  style: theme.textTheme.bodySmall),
            ],
          ],
        ),
      ),
    );
  }

}
