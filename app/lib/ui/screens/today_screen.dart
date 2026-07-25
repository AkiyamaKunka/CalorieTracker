/// Today: running totals header (spec §5.1), typical-day line, today's meal
/// cards, pull-to-refresh, and the chat-style correction box feeding the NL
/// executor (spec §4).
library;

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../format.dart';
import '../nl_presenter.dart';
import '../widgets/meal_card.dart';
import 'meal_editor_screen.dart';

class TodayScreen extends StatefulWidget {
  final MealsDao dao;
  final NlExecutor executor;
  const TodayScreen({super.key, required this.dao, required this.executor});

  @override
  State<TodayScreen> createState() => TodayScreenState();
}

class TodayScreenState extends State<TodayScreen> {
  final TextEditingController _chatController = TextEditingController();
  bool _loading = true;
  bool _sending = false;
  String? _error;
  List<Meal> _todayMeals = const [];
  Map<String, num> _priorDayTotals = const {};

  @override
  void initState() {
    super.initState();
    reload();
  }

  @override
  void dispose() {
    _chatController.dispose();
    super.dispose();
  }

  Future<void> reload() async {
    final now = DateTime.now();
    final today = isoDate(now);
    try {
      final todayMeals = await widget.dao.mealsBetween(today, today);
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
    if (mounted) await reload();
  }

  Future<void> _sendCorrection() async {
    final text = _chatController.text.trim();
    if (text.isEmpty || _sending) return;
    setState(() => _sending = true);
    try {
      final replies = await widget.executor.handleText(text);
      _chatController.clear();
      if (!mounted) return;
      // Sending is over once the executor returns; the reply presentation
      // (incl. the blocking delete-confirmation modal) is not "sending".
      setState(() => _sending = false);
      await presentNlReplies(context, widget.executor, replies);
      await reload();
    } catch (e) {
      if (!mounted) return;
      // Executor is spec'd never to throw (§4.9); belt-and-braces anyway.
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('That request failed: $e')));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Expanded(child: _body(context)),
        _correctionBox(context),
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
            const Padding(
              padding: EdgeInsets.all(32),
              child: Center(
                // Empty copy per spec §5.1.
                child: Text('No meals logged yet today.'),
              ),
            )
          else
            for (final meal in foodMeals)
              MealCard(meal: meal, onTap: () => _editMeal(meal)),
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

  /// Chat-style correction box: free text → NlExecutor → snackbar/dialog
  /// replies including the delete-confirmation modal (spec §4).
  Widget _correctionBox(BuildContext context) {
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
        child: Row(
          children: [
            Expanded(
              child: TextField(
                key: const Key('correctionField'),
                controller: _chatController,
                enabled: !_sending,
                textInputAction: TextInputAction.send,
                onSubmitted: (_) => _sendCorrection(),
                decoration: const InputDecoration(
                  hintText: 'e.g. "change meal 2 to roast duck rice"',
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
              ),
            ),
            const SizedBox(width: 8),
            _sending
                ? const Padding(
                    padding: EdgeInsets.all(8),
                    child: SizedBox(
                        width: 24,
                        height: 24,
                        child: CircularProgressIndicator(strokeWidth: 2)),
                  )
                : IconButton.filled(
                    key: const Key('correctionSend'),
                    onPressed: _sendCorrection,
                    icon: const Icon(Icons.send),
                    tooltip: 'Send',
                  ),
          ],
        ),
      ),
    );
  }
}
