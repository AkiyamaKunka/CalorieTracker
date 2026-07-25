/// One logged-meal card (spec §5.4 content: description, item lines,
/// ~kcal / P/C/F, corrected marker, time). All analysis reads are coerced —
/// hostile stored shapes must render, not crash (spec §3.5).
library;

import 'package:flutter/material.dart';

import '../../core/coerce.dart';
import '../../core/contracts.dart';
import 'dart:typed_data';

import '../format.dart';
import 'meal_thumb.dart';

class MealCard extends StatelessWidget {
  final Meal meal;
  final bool showItems;

  /// Optional drill-in (Today taps through to the editor). Null keeps the
  /// card inert, which is what the report/notification-style usages want.
  final VoidCallback? onTap;

  /// Resolved photo thumbnail; null skips the leading image entirely
  /// (tests / contexts without a resolver).
  final Future<Uint8List?>? thumb;
  const MealCard(
      {super.key,
      required this.meal,
      this.showItems = true,
      this.onTap,
      this.thumb});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final a = meal.analysis;
    final items = safeFoodItems(a); // spec §3.5: never iterate the raw field
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                if (thumb != null) ...[
                  MealThumbView(
                      thumb: thumb,
                      isPhotoMeal: meal.imageHash.isNotEmpty),
                  const SizedBox(width: 10),
                ],
                Expanded(
                  child: Text(
                    mealDescription(a),
                    style: theme.textTheme.titleMedium,
                    overflow: TextOverflow.ellipsis,
                    maxLines: 2,
                  ),
                ),
                if (meal.corrected)
                  Padding(
                    padding: const EdgeInsets.only(left: 6),
                    // ✏️ corrected marker (spec §2.2 / §5.2).
                    child: Tooltip(
                      message: 'Corrected',
                      child: Icon(Icons.edit,
                          key: const Key('correctedBadge'),
                          size: 16,
                          color: theme.colorScheme.tertiary),
                    ),
                  ),
                const SizedBox(width: 8),
                Text(meal.time, style: theme.textTheme.bodySmall),
              ],
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 4,
              children: [
                _chip(context, '~${displayTotalCalories(a)} kcal',
                    emphasized: true),
                _chip(context, 'P: ${displayMacro(a, 'total_protein_g')}g'),
                _chip(context, 'C: ${displayMacro(a, 'total_carbs_g')}g'),
                _chip(context, 'F: ${displayMacro(a, 'total_fat_g')}g'),
              ],
            ),
            if (showItems && items.isNotEmpty) ...[
              const SizedBox(height: 8),
              for (final item in items)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(
                    // Item line per §5.4; name stringified, missing → '?'.
                    '• ${item['name'] ?? '?'}: '
                    '~${displayItemCalories(item['estimated_calories'])} kcal',
                    style: theme.textTheme.bodySmall,
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Widget _chip(BuildContext context, String label, {bool emphasized = false}) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: emphasized ? scheme.primaryContainer : scheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(label, style: Theme.of(context).textTheme.labelSmall),
    );
  }
}
