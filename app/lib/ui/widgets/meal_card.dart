/// One logged-meal card (spec §5.4 content: description, item lines,
/// ~kcal / P/C/F, corrected marker, time). All analysis reads are coerced —
/// hostile stored shapes must render, not crash (spec §3.5).
library;

import 'package:flutter/material.dart';

import '../../core/coerce.dart';
import '../../core/contracts.dart';
import 'dart:typed_data';

import '../format.dart';
import 'grouped.dart' show GroupedCard;
import 'macro_chart.dart' show MacroPalette;
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
    return GroupedCard(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 0),
      onTap: onTap,
      child: Padding(
          padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                // Photo meals only: for text/manual meals the old
                // placeholder block read as a failed image load (panel,
                // cycle 1) — the title owns the width instead.
                if (thumb != null && meal.imageHash.isNotEmpty) ...[
                  MealThumbView(
                      thumb: thumb,
                      isPhotoMeal: true),
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
                if (onTap != null) ...[
                  const SizedBox(width: 2),
                  Icon(Icons.chevron_right,
                      size: 16,
                      color: theme.colorScheme.onSurfaceVariant
                          .withValues(alpha: 0.6)),
                ],
              ],
            ),
            const SizedBox(height: 8),
            // Macro chips carry their FIXED identity hues (research: Apple's
            // color discipline — the same metric wears the same color on
            // every screen; grey chips made Today color-blind).
            Wrap(
              spacing: 6,
              runSpacing: 4,
              children: [
                _chip(context, '~${displayTotalCalories(a)} kcal',
                    emphasized: true),
                _chip(context, 'P: ${displayMacro(a, 'total_protein_g')}g',
                    tint: MacroPalette.of(context).protein),
                _chip(context, 'C: ${displayMacro(a, 'total_carbs_g')}g',
                    tint: MacroPalette.of(context).carbs),
                _chip(context, 'F: ${displayMacro(a, 'total_fat_g')}g',
                    tint: MacroPalette.of(context).fat),
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
    );
  }

  Widget _chip(BuildContext context, String label,
      {bool emphasized = false, Color? tint}) {
    final scheme = Theme.of(context).colorScheme;
    // Identity never rests on colour ALONE (chart rule): the P/C/F letter
    // stays in the label; the tint is reinforcement.
    // Tint alphas: a11y-measured — 0.14 was compliant but nearly invisible;
    // 0.20 light / 0.25 dark actually reads while keeping >6:1 text headroom.
    final dark = Theme.of(context).brightness == Brightness.dark;
    // The TOTAL wears solid primary — under the clay seed, primaryContainer
    // and the carb tint became near-twins, dissolving the hierarchy the
    // emphasized chip exists for.
    final bg = emphasized
        ? scheme.primary
        : tint != null
            ? tint.withValues(alpha: dark ? 0.25 : 0.20)
            : scheme.surfaceContainerHighest;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(label,
          style: Theme.of(context).textTheme.labelSmall?.copyWith(
              color: emphasized ? scheme.onPrimary : null,
              fontWeight: emphasized ? FontWeight.w600 : null)),
    );
  }
}
