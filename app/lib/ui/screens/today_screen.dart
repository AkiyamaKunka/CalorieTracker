/// Today: running totals header (spec §5.1), typical-day line, today's meal
/// cards, pull-to-refresh, and the one Meals button (add + fix, spec §4
/// corrections now live in its sheet — the pinned chat bar is gone,
/// 2026-07-31).
library;

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../../services/garmin_client.dart';
import '../format.dart';
import 'dart:io' show File;
import 'dart:ui' as ui show Image;

import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';

import '../l10n.dart';
import '../widgets/calorie_ring.dart';
import '../widgets/day_report.dart';
import '../widgets/grouped.dart' show GroupedCard;
import '../widgets/macro_chart.dart' show MacroPalette;
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

  /// Today's Garmin active burn, via the user's server (spec §9). Injected
  /// (null = feature off): Today must not know how the server is reached,
  /// only that a day may have an active-burn number.
  final GarminDailyFetch? garminDaily;
  const TodayScreen(
      {super.key,
      required this.dao,
      required this.executor,
      this.thumbs,
      this.onAdd,
      this.garminDaily});

  @override
  State<TodayScreen> createState() => TodayScreenState();
}

class TodayScreenState extends State<TodayScreen> {
  bool _loading = true;
  String? _error;
  List<Meal> _todayMeals = const [];
  Map<String, num> _priorDayTotals = const {};
  GarminDaily? _garmin;

  @override
  void initState() {
    super.initState();
    reload();
  }

  /// Fire-and-forget: a cosmetic line must never delay the meal list. The
  /// date guard drops a reply that arrives after midnight rolled the day.
  void _refreshGarmin(String today) {
    final fetch = widget.garminDaily;
    if (fetch == null) return;
    fetch(today).then((daily) {
      if (!mounted || isoDate(DateTime.now()) != today) return;
      setState(() => _garmin = daily);
    }, onError: (Object _) {});
  }

  Future<void> reload() async {
    final now = DateTime.now();
    final today = isoDate(now);
    _refreshGarmin(today);
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
              label: Text(context.l10n.fabLog),
            ),
          ),
      ],
    );
  }

  /// iOS-style collapsing large title above whatever [child] slivers show.
  Widget _withTitle(BuildContext context, List<Widget> slivers,
      {bool scrollable = true}) {
    return CustomScrollView(
      physics: scrollable
          ? const AlwaysScrollableScrollPhysics()
          : const NeverScrollableScrollPhysics(),
      slivers: [
        SliverAppBar.large(
          title: Text(context.l10n.tabToday),
          actions: [
            // Report-to-my-coach (user request 2026-08-02): the whole day
            // as ONE tall image, straight into the share sheet.
            if (_todayMeals.where(isFoodMeal).isNotEmpty)
              IconButton(
                key: const Key('shareDayButton'),
                icon: _sharing
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2))
                    : const Icon(Icons.ios_share),
                tooltip: context.l10n.shareDayTooltip,
                onPressed: _sharing ? null : _shareDay,
              ),
          ],
        ),
        ...slivers,
      ],
    );
  }

  bool _sharing = false;

  /// Render today as one tall PNG and open the system share sheet.
  Future<void> _shareDay() async {
    setState(() => _sharing = true);
    final theme = Theme.of(context); // captured before any await
    final locale = Localizations.localeOf(context);
    try {
      final today = isoDate(DateTime.now());
      final foodMeals = _todayMeals.where(isFoodMeal).toList();
      // Pre-decode every thumbnail so the report paints in one frame.
      final report = <ReportMeal>[];
      for (final meal in foodMeals) {
        ui.Image? thumb;
        if (meal.imageHash.isNotEmpty) {
          thumb = await decodeThumb(await widget.dao.mealThumb(meal.id));
        }
        report.add(ReportMeal(meal: meal, thumb: thumb));
      }
      final png = await renderDayReport(
        date: today,
        meals: report,
        typicalKcal: typicalDayKcal(_priorDayTotals),
        burnKcal: _garmin?.activeCalories ?? 0,
        theme: theme,
        locale: locale,
      );
      final dir = await getTemporaryDirectory();
      final file = File('${dir.path}/intake_$today.png');
      await file.writeAsBytes(png, flush: true);
      if (!mounted) return;
      await SharePlus.instance.share(ShareParams(
        files: [XFile(file.path, mimeType: 'image/png')],
        subject: 'Daily intake $today',
      ));
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text(context.l10n.shareDayFailed('$e'))));
      }
    } finally {
      if (mounted) setState(() => _sharing = false);
    }
  }

  Widget _body(BuildContext context) {
    if (_loading) {
      return _withTitle(context,
          [const SliverFillRemaining(
              child: Center(child: CircularProgressIndicator()))],
          scrollable: false);
    }
    if (_error != null) {
      return _errorBody(context);
    }
    return _loadedBody(context);
  }

  Widget _errorBody(BuildContext context) {
    return _withTitle(context, [
      SliverFillRemaining(
        child: Center(
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
        ),
      ),
    ]);
  }

  Widget _loadedBody(BuildContext context) {
    final foodMeals = _todayMeals.where(isFoodMeal).toList();
    return RefreshIndicator(
      onRefresh: reload,
      child: _withTitle(context, [
        SliverPadding(
          padding: const EdgeInsets.only(bottom: 96),
          sliver: SliverList.list(children: [
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
                    Text(context.l10n.todayEmptyTitle),
                    const SizedBox(height: 8),
                    Text(
                      context.l10n.todayEmptyHint,
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
          ]),
        ),
      ]),
    );
  }

  Widget _totalsHeader(BuildContext context, List<Meal> foodMeals) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final palette = MacroPalette.of(context);
    final totals = todayTotals(foodMeals); // Σ safeNumber, spec §5.1
    final typical = typicalDayKcal(_priorDayTotals);
    final burn = _garmin?.activeCalories ?? 0;
    String? typicalLine;
    if (typical != null) {
      final total = totals.cal.round();
      // Headroom vs above-typical wording, spec §5.1 — the string is
      // parity-pinned; the ring VISUALIZES it, the caption still states it.
      typicalLine = total <= typical
          ? context.l10n.typicalDayHeadroom(
              formatKcal(typical), formatKcal(typical - total))
          : context.l10n.typicalDayOver(
              formatKcal(typical), formatKcal(total - typical));
    }
    // Hero layout (research: MFP arithmetic × Apple ring — see
    // uiux_direction.md): ring answers "how am I doing", the rows beside it
    // show the arithmetic that produced the answer, macro trio beneath.
    return GroupedCard(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                CalorieRing(
                    eatenKcal: totals.cal,
                    typicalKcal: typical,
                    burnKcal: burn),
                const SizedBox(width: 16),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      // Demoted from headline (review: one hero numeral per
                      // card — the ring's center). Key + string format stay.
                      Text(context.l10n.kcalAmount(formatKcal(totals.cal)),
                          key: const Key('todayTotalKcal'),
                          style: theme.textTheme.titleMedium),
                      Text(context.l10n.mealsToday(totals.meals),
                          style: theme.textTheme.bodySmall?.copyWith(
                              color: scheme.onSurfaceVariant)),
                      const SizedBox(height: 8),
                      // MFP's transparent arithmetic — and it RECONCILES:
                      // typical + burn − eaten IS the ring's center number.
                      if (typical != null) ...[
                        ArithmeticRow(
                            label: context.l10n.rowTypical,
                            value: '~${formatKcal(typical)}',
                            dot: scheme.outlineVariant),
                        if (burn > 0)
                          ArithmeticRow(
                              label: context.l10n.rowBurn,
                              value: '+${formatKcal(burn)}',
                              dot: scheme.tertiary),
                        ArithmeticRow(
                            label: context.l10n.rowEaten,
                            value: '−${formatKcal(totals.cal.round())}',
                            dot: scheme.primary),
                        ArithmeticRow(
                            key: const Key('arithmeticResultRow'),
                            label: totals.cal.round() >
                                    typical + burn.round()
                                ? context.l10n.rowResultOver
                                : context.l10n.rowResultLeft,
                            value: formatKcal(
                                (typical + burn.round() - totals.cal.round())
                                    .abs()),
                            dot: Colors.transparent,
                            emphasized: true),
                      ] else
                        ArithmeticRow(
                            label: context.l10n.rowEaten,
                            value: formatKcal(totals.cal.round()),
                            dot: scheme.primary),
                    ],
                  ),
                ),
              ],
            ),
            const SizedBox(height: 14),
            MacroTrio(
              proteinG: totals.protein,
              carbsG: totals.carbs,
              fatG: totals.fat,
              proteinColor: palette.protein,
              carbsColor: palette.carbs,
              fatColor: palette.fat,
            ),
            if (typicalLine != null) ...[
              const SizedBox(height: 10),
              Text(typicalLine,
                  key: const Key('typicalDayLine'),
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: scheme.onSurfaceVariant)),
            ],
            if (_garmin != null && _garmin!.activeCalories > 0) ...[
              const SizedBox(height: 4),
              Text(
                'Active burn: ~${formatKcal(_garmin!.activeCalories)} kcal '
                '(Garmin) · net ~${formatKcal(totals.cal - _garmin!.activeCalories)} kcal',
                key: const Key('garminBurnLine'),
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: scheme.onSurfaceVariant),
              ),
            ],
          ],
        ),
      ),
    );
  }

}
