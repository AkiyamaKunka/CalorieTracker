/// The Today hero ring: eaten vs the day's budget, remaining in the center.
///
/// Research-derived (scratchpad/uiux_direction.md): every leading health app
/// answers "how am I doing?" with ONE glanceable element, and MFP's 20-M-user
/// muscle memory is the transparent arithmetic `budget − food + exercise =
/// remaining`. Ours: budget = typical-day median + Garmin active burn, and
/// the rows beside the ring VISIBLY produce the center number (review panel:
/// arithmetic that doesn't reconcile is worse than no arithmetic).
///
/// Design rules: single hue for the intake arc (primary — macros keep their
/// own fixed hues elsewhere), NO shame state (over renders in the same calm
/// hue at full sweep, the "+over" stated in text; red guilt is the #1
/// researched complaint driver), hand-drawn painter (no packages).
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../format.dart';
import '../l10n.dart';

class CalorieRing extends StatelessWidget {
  const CalorieRing({
    super.key,
    required this.eatenKcal,
    required this.typicalKcal,
    this.burnKcal = 0,
    this.size = 132,
  });

  final num eatenKcal;

  /// Null until ≥2 prior days have data (spec §5.1) — the ring then draws
  /// a neutral full track with the eaten number centered, no fraction.
  final num? typicalKcal;

  /// Garmin active burn: EXTENDS the budget (MFP framing — exercise gives
  /// calories back). Zero when absent; never shown as an operand elsewhere
  /// without also being folded in here.
  final num burnKcal;
  final double size;

  /// One rounded integer per quantity: the ring, the rows and the caption
  /// must never disagree by a rounding path (review panel: a card that
  /// says both "+0 over" and "0 headroom" is lying twice).
  int get _eaten => eatenKcal.round();
  int? get _budget =>
      typicalKcal == null ? null : (typicalKcal!.round() + burnKcal.round());

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final budget = _budget;
    final frac = (budget == null || budget <= 0)
        ? null
        : (_eaten / budget).clamp(0.0, 1.0).toDouble();
    final over = budget != null && _eaten > budget;
    final centerBig = budget == null
        ? formatKcal(_eaten)
        : over
            ? '+${formatKcal(_eaten - budget)}'
            : formatKcal(budget - _eaten);
    final l = context.l10n;
    final centerSmall = budget == null
        ? l.ringKcalToday
        : over
            ? l.ringAboveTypical // matches the caption's wording
            : burnKcal.round() > 0
                ? l.ringLeftToday
                : l.ringHeadroom;
    final semantics = budget == null
        ? '${formatKcal(_eaten)} kcal eaten today'
        : over
            ? '${formatKcal(_eaten - budget)} kcal above typical'
            : '${formatKcal(budget - _eaten)} kcal left today, of a '
                '${formatKcal(budget)} kcal typical day';
    return SizedBox(
      width: size,
      height: size,
      child: CustomPaint(
        painter: _RingPainter(
          fraction: frac,
          arc: scheme.primary,
          // The track is the DENOMINATOR ("how much of a typical day") —
          // a11y-measured: surfaceContainerHighest sits under 1.4:1 against
          // the new card surface; a 20% onSurface blend clears 3:1 in both
          // themes.
          track: Color.alphaBlend(scheme.onSurface.withValues(alpha: 0.20),
              scheme.surfaceContainerLow),
        ),
        child: Center(
          // FittedBox: the ring is a fixed box; large system font scales
          // (or 5-digit values) must shrink to fit, never clip the arc.
          child: Semantics(
            label: semantics,
            excludeSemantics: true,
            child: FittedBox(
              fit: BoxFit.scaleDown,
              child: Padding(
                padding: EdgeInsets.symmetric(horizontal: size * 0.14),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(centerBig,
                        key: const Key('ringCenterValue'),
                        maxLines: 1,
                        softWrap: false,
                        style: theme.textTheme.headlineMedium),
                    Text(centerSmall,
                        maxLines: 1,
                        softWrap: false,
                        style: theme.textTheme.labelSmall
                            ?.copyWith(color: scheme.onSurfaceVariant)),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _RingPainter extends CustomPainter {
  _RingPainter({
    required this.fraction,
    required this.arc,
    required this.track,
  });

  final double? fraction; // null → indeterminate day (no typical yet)
  final Color arc;
  final Color track;

  @override
  void paint(Canvas canvas, Size size) {
    const stroke = 11.0;
    final center = size.center(Offset.zero);
    final radius = (math.min(size.width, size.height) - stroke) / 2;
    final rect = Rect.fromCircle(center: center, radius: radius);

    canvas.drawCircle(
        center,
        radius,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = stroke
          ..color = track);
    final f = fraction;
    if (f == null || f <= 0) return;
    // Start at 12 o'clock, sweep clockwise; rounded ends read as progress,
    // not pie. At/over 100% the arc simply closes — calm, complete, no red.
    canvas.drawArc(
        rect,
        -math.pi / 2,
        2 * math.pi * f,
        false,
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = stroke
          ..strokeCap = f >= 1.0 ? StrokeCap.butt : StrokeCap.round
          ..color = arc);
  }

  @override
  bool shouldRepaint(_RingPainter old) =>
      old.fraction != fraction || old.arc != arc || old.track != track;
}

/// One row of the transparent arithmetic beside the ring:
/// a fixed-hue dot, a label, and a right-aligned tabular number.
class ArithmeticRow extends StatelessWidget {
  const ArithmeticRow({
    super.key,
    required this.label,
    required this.value,
    required this.dot,
    this.emphasized = false,
  });
  final String label;
  final String value;
  final Color dot;

  /// The `=` result row: the number the ring's center restates.
  final bool emphasized;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(color: dot, shape: BoxShape.circle),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ),
          // FittedBox: at narrow widths × large font scale the number wins
          // over the label, shrinking instead of overflowing the row.
          FittedBox(
            fit: BoxFit.scaleDown,
            child: Text(value,
                maxLines: 1,
                softWrap: false,
                style: (emphasized
                        ? theme.textTheme.titleSmall
                        : theme.textTheme.labelLarge)
                    ?.copyWith(
                        fontFeatures: const [FontFeature.tabularFigures()])),
          ),
        ],
      ),
    );
  }
}

/// The macro trio: three fixed-hue mini-bars (Cronometer's tiering —
/// ring first, three bars second, full depth on the detail screens).
class MacroTrio extends StatelessWidget {
  const MacroTrio({
    super.key,
    required this.proteinG,
    required this.carbsG,
    required this.fatG,
    required this.proteinColor,
    required this.carbsColor,
    required this.fatColor,
  });
  final num proteinG;
  final num carbsG;
  final num fatG;
  final Color proteinColor;
  final Color carbsColor;
  final Color fatColor;

  @override
  Widget build(BuildContext context) {
    // Bars fill by CALORIE share (Atwater 4/4/9), matching the detail
    // screen's macro bar — the codebase's own rule: a gram-proportional
    // bar "would misstate what the meal is made of". Labels stay grams.
    final pKcal = math.max(0, proteinG) * 4;
    final cKcal = math.max(0, carbsG) * 4;
    final fKcal = math.max(0, fatG) * 9;
    final totalKcal = pKcal + cKcal + fKcal;
    final theme = Theme.of(context);
    final trackColor = Color.alphaBlend(
        theme.colorScheme.onSurface.withValues(alpha: 0.20),
        theme.colorScheme.surfaceContainerLow);
    Widget bar(String label, num g, num kcal, Color color) {
      final frac =
          totalKcal <= 0 ? 0.0 : (kcal / totalKcal).clamp(0.0, 1.0).toDouble();
      return Expanded(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('$label ${g.round()}g',
                style: theme.textTheme.labelSmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            const SizedBox(height: 4),
            ClipRRect(
              borderRadius: BorderRadius.circular(3),
              child: SizedBox(
                height: 6,
                child: LayoutBuilder(
                  builder: (context, c) => Stack(children: [
                    Container(width: c.maxWidth, color: trackColor),
                    Container(
                        width: math.max(frac * c.maxWidth, g > 0 ? 6.0 : 0.0),
                        color: color),
                  ]),
                ),
              ),
            ),
          ],
        ),
      );
    }

    return Row(
      key: const Key('macroTrio'),
      children: [
        bar('P', proteinG, pKcal, proteinColor),
        const SizedBox(width: 10),
        bar('C', carbsG, cKcal, carbsColor),
        const SizedBox(width: 10),
        bar('F', fatG, fKcal, fatColor),
      ],
    );
  }
}
