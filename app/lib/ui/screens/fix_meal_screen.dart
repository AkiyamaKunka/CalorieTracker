/// Fix/delete meals by free text (spec §4 corrections), reached from the
/// Add-meal sheet. This USED to be a chat bar pinned to the bottom of
/// Today — the user's verdict (2026-07-31): delete the bar, fold the
/// feature into the one meals button. One entry point, zero standing
/// screen clutter.
library;

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../nl_presenter.dart';

class FixMealScreen extends StatefulWidget {
  final NlExecutor executor;
  const FixMealScreen({super.key, required this.executor});

  @override
  State<FixMealScreen> createState() => _FixMealScreenState();
}

class _FixMealScreenState extends State<FixMealScreen> {
  final TextEditingController _controller = TextEditingController();
  bool _sending = false;
  final List<String> _log = []; // sent requests, newest last

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _sending) return;
    setState(() => _sending = true);
    try {
      final replies = await widget.executor.handleText(text);
      _controller.clear();
      if (!mounted) return;
      // Sending ends when the executor returns; presentation (incl. the
      // blocking delete-confirmation modal) is not "sending".
      setState(() {
        _sending = false;
        _log.add(text);
      });
      await presentNlReplies(context, widget.executor, replies);
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
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('Fix a meal')),
      // Scrollable for the same reason as AddTextScreen: autofocused
      // keyboard + multi-line refusal messages must never push the send
      // button off-screen.
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              'Say what to change, move, or delete — describe the meal '
              'however you like ("the noodles", "breakfast", "the 600 '
              'kcal one"), in any language.',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
            const SizedBox(height: 6),
            // The old copy promised "the meal numbers match the Today
            // list". They do not: Today renders no numbers at all, and the
            // model resolves an index against a SEVEN-DAY window led by
            // earlier days — so "meal 2" could rewrite yesterday's lunch
            // (review 2026-07-31). Describing the dish is unambiguous.
            Text(
              'Naming the food is safest — meal numbers count across the '
              'last 7 days, not just today.',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
            const SizedBox(height: 12),
            TextField(
              key: const Key('fixMealField'),
              controller: _controller,
              maxLines: 3,
              autofocus: true,
              enabled: !_sending,
              textInputAction: TextInputAction.send,
              onSubmitted: (_) => _send(),
              decoration: const InputDecoration(
                labelText: 'What should change?',
                hintText: 'e.g. "the noodles were roast duck rice"\n'
                    'or "删除刚才那杯咖啡"',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            FilledButton.icon(
              key: const Key('fixMealSend'),
              onPressed: _sending ? null : _send,
              icon: _sending
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.build_outlined),
              label: Text(_sending ? 'Working…' : 'Apply the fix'),
            ),
            if (_log.isNotEmpty) ...[
              const SizedBox(height: 20),
              Text('Applied this session',
                  style: theme.textTheme.titleSmall),
              for (final line in _log.reversed)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text('• $line', style: theme.textTheme.bodySmall),
                ),
            ],
          ],
        ),
      ),
    );
  }
}
