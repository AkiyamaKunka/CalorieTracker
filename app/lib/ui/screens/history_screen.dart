/// History: 30-day per-day calorie totals (spec §5.3 — inclusive window
/// [today−29, today], days sorted descending, average over days WITH data).
library;

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../format.dart';

class HistoryScreen extends StatefulWidget {
  final MealsDao dao;
  final int days;
  const HistoryScreen({super.key, required this.dao, this.days = 30});

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

  @override
  Widget build(BuildContext context) {
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
    if (_perDay.isEmpty) {
      return Center(
        // Empty copy per spec §5.3.
        child: Text('No meals logged in the past ${widget.days} days.'),
      );
    }
    final dates = _perDay.keys.toList()..sort((a, b) => b.compareTo(a));
    // Average over days that have data, int truncation (spec §5.3).
    final sum = _perDay.values.fold<num>(0, (a, b) => a + b);
    final avg = (sum / _perDay.length).truncate();
    return RefreshIndicator(
      onRefresh: reload,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: Text(
              'Average: ~${formatKcal(avg)} kcal / day',
              key: const Key('historyAverage'),
              style: Theme.of(context).textTheme.titleMedium,
            ),
          ),
          for (final date in dates)
            ListTile(
              dense: true,
              title: Text(friendlyHistoryDay(date)),
              trailing: Text('~${formatKcal(_perDay[date]!)} kcal'),
            ),
        ],
      ),
    );
  }
}
