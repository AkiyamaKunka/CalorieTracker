/// The Today hero ring: eaten vs typical-day, headroom in the center.
///
/// Research-derived (scratchpad/uiux_direction.md): every leading health app
/// answers "how am I doing?" with ONE glanceable element — MFP's budget
/// arithmetic, Apple's rings, Yazio's remaining-ring. Ours fuses the two
/// best: a ring whose track is the user's own typical-day median (honestly
/// derived, not goal-preached) and MFP's transparent arithmetic beside it.
///
/// Design rules: single hue for the intake arc (primary — macros keep their
/// own fixed hues elsewhere), NO shame state (over-typical renders in the
/// same calm hue at full sweep with the "+over" stated in text; red guilt is
/// the #1 researched complaint driver), hand-drawn painter (no packages).
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

class CalorieRing extends StatelessWidget {
  const CalorieRing({
    super.key,
    required this.eatenKcal,
    required this.typicalKcal,
    this.size = 132,
  });

  final num eatenKcal;

  /// Null until ≥2 prior days have data (spec §5.1) — the ring then draws
  /// a neutral full track with the eaten number centered, no fraction.
  final num? typicalKcal;
  final double size;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final typical = typicalKcal;
    final frac = (typical == null || typical <= 0)
        ? null
        : (eatenKcal / typical).clamp(0.0, 1.0).toDouble();
    final over = typical != null && eatenKcal > typical;
    final centerBig = typical == null
        ? _kcal(eatenKcal)
        : over
            ? '+${_kcal(eatenKcal - typical)}'
            : _kcal(typical - eatenKcal);
    final centerSmall = typical == null
        ? 'kcal today'
        : over
            ? 'over typical'
            : 'headroom';
    return SizedBox(
      width: size,
      height: size,
      child: CustomPaint(
        painter: _RingPainter(
          fraction: frac,
          arc: scheme.primary,
          track: scheme.surfaceContainerHighest,
          overGlyph: over,
        ),
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(centerBig,
                  key: const Key('ringCenterValue'),
                  style: theme.textTheme.headlineMedium),
              Text(centerSmall,
                  style: theme.textTheme.labelSmall
                      ?.copyWith(color: scheme.onSurfaceVariant)),
            ],
          ),
        ),
      ),
    );
  }

  static String _kcal(num v) {
    final r = v.round();
    final s = r.abs().toString();
    final b = StringBuffer();
    for (var i = 0; i < s.length; i++) {
      if (i > 0 && (s.length - i) % 3 == 0) b.write(',');
      b.write(s[i]);
    }
    return '${r < 0 ? '-' : ''}$b';
  }
}

class _RingPainter extends CustomPainter {
  _RingPainter({
    required this.fraction,
    required this.arc,
    required this.track,
    required this.overGlyph,
  });

  final double? fraction; // null → indeterminate day (no typical yet)
  final Color arc;
  final Color track;
  final bool overGlyph;

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
      old.fraction != fraction ||
      old.arc != arc ||
      old.track != track ||
      old.overGlyph != overGlyph;
}

/// One row of the transparent arithmetic beside the ring:
/// a fixed-hue dot, a label, and a right-aligned tabular number.
class ArithmeticRow extends StatelessWidget {
  const ArithmeticRow({
    super.key,
    required this.label,
    required this.value,
    required this.dot,
  });
  final String label;
  final String value;
  final Color dot;

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
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          ),
          Text(value,
              style: theme.textTheme.labelLarge?.copyWith(
                  fontFeatures: const [FontFeature.tabularFigures()])),
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
    // Bars scale against the LARGEST of the three — relative composition,
    // not targets (we don't preach macro goals; we show what happened).
    final maxG = [proteinG, carbsG, fatG]
        .fold<num>(0, (a, b) => math.max(a, b.clamp(0, double.infinity)));
    Widget bar(String label, num g, Color color) {
      final frac = maxG <= 0 ? 0.0 : (g / maxG).clamp(0.0, 1.0).toDouble();
      final theme = Theme.of(context);
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
                    Container(
                        width: c.maxWidth,
                        color: theme.colorScheme.surfaceContainerHighest),
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
        bar('P', proteinG, proteinColor),
        const SizedBox(width: 10),
        bar('C', carbsG, carbsColor),
        const SizedBox(width: 10),
        bar('F', fatG, fatColor),
      ],
    );
  }
}
