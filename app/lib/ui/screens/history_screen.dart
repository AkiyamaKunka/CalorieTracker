/// History: 30-day per-day calorie totals (spec §5.3 — inclusive window
/// [today−29, today], days sorted descending, average over days WITH data).
library;

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../format.dart';
import '../meal_thumbs.dart';
import '../widgets/macro_chart.dart';
import 'day_detail_screen.dart';

class HistoryScreen extends StatefulWidget {
  final MealsDao dao;
  final int days;
  final MealThumbResolver? thumbs;
  const HistoryScreen(
      {super.key, required this.dao, this.days = 30, this.thumbs});

  @override
  State<HistoryScreen> createState() => HistoryScreenState();
}

/// Public so the shell can trigger [reload] when the tab is (re)selected:
/// this screen lives in an IndexedStack, so initState runs once at app
/// launch — without an external reload it would show launch-time data
/// forever (and the empty state has no RefreshIndicator to recover with).
class HistoryScreenState extends State<HistoryScreen> {
  bool _loading = true;
  String? _error;
  Map<String, num> _perDay = const {};

  @override
  void initState() {
    super.initState();
    reload();
  }

  Future<void> reload() async {
    final now = DateTime.now();
    try {
      final meals = await widget.dao.mealsBetween(
        isoDate(now.subtract(Duration(days: widget.days - 1))),
        isoDate(now),
      );
      if (!mounted) return;
      setState(() {
        _perDay = dailyCalorieTotals(meals);
        _loading = false;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = 'Could not load history: $e';
      });
    }
  }

  /// Day drill-down. Always reloads on return: edits, adds and deletes all
  /// change this list, and a local query is far cheaper than plumbing a
  /// result back through a system back-gesture.
  Future<void> _openDay(String date) async {
    await Navigator.of(context).push(MaterialPageRoute(
      builder: (_) =>
          DayDetailScreen(dao: widget.dao, date: date, thumbs: widget.thumbs),
    ));
    if (mounted) await reload();
  }

  num _maxDayKcal = 0;

  /// Weekday span w600, remainder quiet — same string, richer weight.
  Widget _dayTitle(BuildContext context, String date, {required bool dimmed}) {
    final theme = Theme.of(context);
    final text = friendlyHistoryDay(date);
    final comma = text.indexOf(', ');
    final dimColor = theme.colorScheme.onSurfaceVariant;
    if (dimmed || comma < 0) {
      return Text(text,
          style: dimmed ? TextStyle(color: dimColor) : null);
    }
    return Text.rich(TextSpan(children: [
      TextSpan(
          text: text.substring(0, comma),
          style: const TextStyle(fontWeight: FontWeight.w600)),
      TextSpan(
          text: text.substring(comma),
          style: TextStyle(color: dimColor)),
    ]));
  }

  /// One day: value + a thin magnitude track beneath it (research: a
  /// number-only list hides the week's shape; MacroFactor/Cronometer put a
  /// visual scale in every row). Gap days stay dimmed text-only.
  Widget _dayRow(BuildContext context, String date) {
    final theme = Theme.of(context);
    final has = _perDay.containsKey(date);
    final kcal = _perDay[date];
    final frac = (!has || _maxDayKcal <= 0)
        ? 0.0
        : (kcal! / _maxDayKcal).clamp(0.0, 1.0).toDouble();
    return ListTile(
      key: Key('historyDay$date'),
      dense: true,
      // Weekday emphasized, rest quiet (research: the week's rhythm should
      // be scannable). String content unchanged — split, not reworded.
      title: _dayTitle(context, date, dimmed: !has),
      subtitle: has
          ? Padding(
              padding: const EdgeInsets.only(top: 4),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(2),
                child: SizedBox(
                  height: 4,
                  child: LayoutBuilder(
                    builder: (context, c) => Stack(children: [
                      Container(
                          width: c.maxWidth,
                          // a11y-measured: surfaceContainerHighest is
                          // <2:1 against the surface here; 20% onSurface
                          // blend clears 3:1 in both themes.
                          color: Color.alphaBlend(
                              theme.colorScheme.onSurface
                                  .withValues(alpha: 0.20),
                              theme.colorScheme.surface)),
                      Container(
                          width: frac * c.maxWidth,
                          // Full primary (was 55% alpha → 1.96:1 light):
                          // also unifies the calories hue with the ring.
                          color: theme.colorScheme.primary),
                    ]),
                  ),
                ),
              ),
            )
          : null,
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          has
              ? Text('~${formatKcal(kcal!)} kcal',
                  style: theme.textTheme.labelLarge?.copyWith(
                      fontFeatures: const [FontFeature.tabularFigures()]))
              : Text('no meals logged',
                  style: theme.textTheme.bodySmall
                      ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          const SizedBox(width: 4),
          const Icon(Icons.chevron_right, size: 18),
        ],
      ),
      // Gap rows open the day too — its + FAB is the remedy.
      onTap: () => _openDay(date),
    );
  }

  Widget _withTitle(List<Widget> slivers, {bool scrollable = true}) {
    return CustomScrollView(
      physics: scrollable
          ? const AlwaysScrollableScrollPhysics()
          : const NeverScrollableScrollPhysics(),
      slivers: [
        const SliverAppBar.large(title: Text('History')),
        ...slivers,
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return _withTitle(
          [const SliverFillRemaining(
              child: Center(child: CircularProgressIndicator()))],
          scrollable: false);
    }
    if (_error != null) {
      return _withTitle([
        SliverFillRemaining(
          child: Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(_error!, textAlign: TextAlign.center),
                  const SizedBox(height: 12),
                  FilledButton(
                      onPressed: reload, child: const Text('Retry')),
                ],
              ),
            ),
          ),
        ),
      ]);
    }
    if (_perDay.isEmpty) {
      return _withTitle([
        SliverFillRemaining(
          child: Center(
            // Empty copy per spec §5.3.
            child:
                Text('No meals logged in the past ${widget.days} days.'),
          ),
        ),
      ]);
    }
    // Interior GAP days are rendered too (dimmed, "no meals logged"):
    // a day the watcher missed used to simply vanish, and spotting it
    // required mentally diffing dates across rows. Leading/trailing empty
    // spans stay collapsed so a new user never sees 29 empty rows. The
    // newest edge extends to TODAY: "nothing logged yet today/yesterday"
    // is exactly the gap worth noticing.
    final logged = _perDay.keys.toList()..sort();
    final today = DateTime.now();
    var end = DateTime(today.year, today.month, today.day);
    // The §5.3 query window ends at today, so logged.last can exceed
    // `end` only through clock skew between reload and this build — the
    // guard keeps the span covering the data even then.
    final newest = DateTime.parse(logged.last);
    if (newest.isAfter(end)) end = newest;
    final dates = <String>[];
    final start = DateTime.parse(logged.first);
    // Day+1 through the CONSTRUCTOR, not Duration(days: 1): a 24h add
    // drifts across DST in locales that have it and could duplicate or
    // skip an isoDate.
    for (var d = start;
        !d.isAfter(end);
        d = DateTime(d.year, d.month, d.day + 1)) {
      dates.add(isoDate(d));
    }
    dates.sort((a, b) => b.compareTo(a));
    // Average over days that have data, int truncation (spec §5.3).
    final sum = _perDay.values.fold<num>(0, (a, b) => a + b);
    final avg = (sum / _perDay.length).truncate();
    _maxDayKcal = _perDay.values
        .fold<num>(0, (a, b) => a > b ? a : b); // row magnitude track scale
    return RefreshIndicator(
      onRefresh: reload,
      child: _withTitle([
        SliverList.list(children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  'Average: ~${formatKcal(avg)} kcal / day',
                  key: const Key('historyAverage'),
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 16),
                // Bars in ascending date order with the average as a dashed
                // reference; tapping a bar opens that day (same target as
                // the rows below).
                CalorieTrendChart(
                  dayTotals: _perDay,
                  onDayTap: (date) => _openDay(date),
                ),
              ],
            ),
          ),
          for (final date in dates)
            _dayRow(context, date),
        ]),
      ]),
    );
  }
}
