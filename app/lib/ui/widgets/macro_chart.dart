/// Charts for the meal detail / history screens.
///
/// Hand-drawn with CustomPainter — no chart dependency (the app ships to
/// phones on a metered build pipeline; a 3-mark chart does not justify a
/// package). Design rules applied from the dataviz guidance:
///   - categorical hues in FIXED order, taken from the validated reference
///     palette's first three slots (all-pairs CVD-safe in both modes);
///     protein=blue, carbs=orange, fat=aqua, never cycled or reordered.
///   - identity is never colour-alone: every segment carries a direct label
///     and the legend is always present (the light-mode aqua step sits under
///     3:1 on-surface contrast, so the relief rule requires visible labels).
///   - dark mode uses its OWN steps from the same ramps, not a flipped fill.
///   - thin marks, 2px surface gaps between fills, 4px rounded data ends,
///     recessive axes, selective (not per-point) value labels.
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

/// Macro identity colours. Index order is the contract — callers must not
/// reassign hues per screen (colour follows the entity, never its rank).
class MacroPalette {
  const MacroPalette({
    required this.protein,
    required this.carbs,
    required this.fat,
  });

  final Color protein;
  final Color carbs;
  final Color fat;

  static const MacroPalette light = MacroPalette(
    protein: Color(0xFF2A78D6),
    carbs: Color(0xFFEB6834),
    fat: Color(0xFF1BAF7A),
  );

  static const MacroPalette dark = MacroPalette(
    protein: Color(0xFF3987E5),
    carbs: Color(0xFFD95926),
    fat: Color(0xFF199E70),
  );

  static MacroPalette of(BuildContext context) =>
      Theme.of(context).brightness == Brightness.dark ? dark : light;
}

/// Energy per gram (Atwater): the macro bar segments by CALORIE share, not
/// gram share — 1 g of fat carries 2.25× the energy of 1 g of protein, so a
/// gram-proportional bar would misstate what the meal is made of.
const num kcalPerGramProtein = 4;
const num kcalPerGramCarbs = 4;
const num kcalPerGramFat = 9;

/// One macro's contribution, already resolved to grams + energy.
class MacroSlice {
  const MacroSlice(this.label, this.grams, this.kcal, this.color);
  final String label;
  final num grams;
  final num kcal;
  final Color color;
}

List<MacroSlice> macroSlices({
  required num proteinG,
  required num carbsG,
  required num fatG,
  required MacroPalette palette,
}) =>
    [
      MacroSlice('Protein', proteinG, proteinG * kcalPerGramProtein,
          palette.protein),
      MacroSlice('Carbs', carbsG, carbsG * kcalPerGramCarbs, palette.carbs),
      MacroSlice('Fat', fatG, fatG * kcalPerGramFat, palette.fat),
    ];

/// Horizontal stacked bar: what this meal's energy is made of.
///
/// Falls back to an explanatory line when every macro is zero/unknown — an
/// empty axis with no marks reads as a rendering bug.
class MacroBreakdownBar extends StatelessWidget {
  const MacroBreakdownBar({
    super.key,
    required this.proteinG,
    required this.carbsG,
    required this.fatG,
    this.height = 18,
  });

  final num proteinG;
  final num carbsG;
  final num fatG;
  final double height;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final slices = macroSlices(
      proteinG: math.max(0, proteinG),
      carbsG: math.max(0, carbsG),
      fatG: math.max(0, fatG),
      palette: MacroPalette.of(context),
    );
    final totalKcal = slices.fold<num>(0, (a, s) => a + s.kcal);
    if (totalKcal <= 0) {
      return Text('No macro breakdown recorded.',
          key: const Key('macroBarEmpty'),
          style: theme.textTheme.bodySmall
              ?.copyWith(color: theme.colorScheme.onSurfaceVariant));
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          height: height,
          width: double.infinity,
          child: CustomPaint(
            key: const Key('macroBar'),
            painter: _StackedBarPainter(
              slices: slices,
              gap: 2, // surface gap between fills
              radius: 4, // rounded data ends
              surface: theme.colorScheme.surface,
            ),
          ),
        ),
        const SizedBox(height: 8),
        // Legend + direct labels in one row: identity never rests on colour,
        // and the numbers wear text tokens rather than the series colour.
        Wrap(
          spacing: 14,
          runSpacing: 4,
          children: [
            for (final s in slices)
              _LegendEntry(
                color: s.color,
                text: '${s.label} ${_g(s.grams)} g'
                    ' · ${(s.kcal / totalKcal * 100).round()}%',
              ),
          ],
        ),
      ],
    );
  }

  static String _g(num v) =>
      v == v.roundToDouble() ? v.round().toString() : v.toStringAsFixed(1);
}

class _LegendEntry extends StatelessWidget {
  const _LegendEntry({required this.color, required this.text});
  final Color color;
  final String text;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 10,
          height: 10,
          decoration:
              BoxDecoration(color: color, borderRadius: BorderRadius.circular(2)),
        ),
        const SizedBox(width: 6),
        Text(text,
            style: theme.textTheme.bodySmall
                ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
      ],
    );
  }
}

class _StackedBarPainter extends CustomPainter {
  _StackedBarPainter({
    required this.slices,
    required this.gap,
    required this.radius,
    required this.surface,
  });

  final List<MacroSlice> slices;
  final double gap;
  final double radius;
  final Color surface;

  @override
  void paint(Canvas canvas, Size size) {
    final visible = slices.where((s) => s.kcal > 0).toList();
    if (visible.isEmpty) return;
    final total = visible.fold<num>(0, (a, s) => a + s.kcal);
    // Reserve the inter-segment gaps so the stack still spans exactly the
    // full width (segments must not drift past the axis).
    final gaps = gap * (visible.length - 1);
    final usable = math.max(0.0, size.width - gaps);
    var x = 0.0;
    for (var i = 0; i < visible.length; i++) {
      final s = visible[i];
      final w = usable * (s.kcal / total);
      final isFirst = i == 0;
      final isLast = i == visible.length - 1;
      final rect = RRect.fromRectAndCorners(
        Rect.fromLTWH(x, 0, w, size.height),
        // Only the outer ends of the whole stack are rounded; interior
        // joins stay square so the bar reads as one quantity.
        topLeft: Radius.circular(isFirst ? radius : 0),
        bottomLeft: Radius.circular(isFirst ? radius : 0),
        topRight: Radius.circular(isLast ? radius : 0),
        bottomRight: Radius.circular(isLast ? radius : 0),
      );
      canvas.drawRRect(rect, Paint()..color = s.color);
      x += w + gap;
    }
  }

  @override
  bool shouldRepaint(_StackedBarPainter old) =>
      old.surface != surface ||
      old.slices.length != slices.length ||
      List.generate(slices.length, (i) => i).any((i) =>
          old.slices[i].kcal != slices[i].kcal ||
          old.slices[i].color != slices[i].color);
}

/// Daily calorie bars over a date window, newest at the right, with the
/// average as a reference line. Single series → one hue, no legend (the
/// title names it); labels only on the extremes and the latest day.
class CalorieTrendChart extends StatelessWidget {
  const CalorieTrendChart({
    super.key,
    required this.dayTotals,
    this.height = 132,
    this.onDayTap,
  });

  /// date (YYYY-MM-DD) → kcal. Rendered in ascending date order.
  final Map<String, num> dayTotals;
  final double height;
  final void Function(String date)? onDayTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (dayTotals.isEmpty) return const SizedBox.shrink();
    final dates = dayTotals.keys.toList()..sort();
    final values = [for (final d in dates) math.max(0, dayTotals[d]!)];
    final maxV = values.fold<num>(0, math.max);
    final avg = values.fold<num>(0, (a, b) => a + b) / values.length;
    return LayoutBuilder(
      builder: (context, constraints) {
        final width = constraints.maxWidth;
        return GestureDetector(
          onTapUp: onDayTap == null
              ? null
              : (details) {
                  final i = _indexAt(details.localPosition.dx, width, dates.length);
                  if (i != null) onDayTap!(dates[i]);
                },
          child: SizedBox(
            height: height,
            width: width,
            child: CustomPaint(
              key: const Key('calorieTrendChart'),
              painter: _TrendPainter(
                values: values,
                maxValue: maxV,
                average: avg,
                bar: MacroPalette.of(context).protein,
                axis: theme.colorScheme.outlineVariant,
                label: theme.colorScheme.onSurfaceVariant,
                textDirection: Directionality.of(context),
              ),
            ),
          ),
        );
      },
    );
  }

  static int? _indexAt(double dx, double width, int count) {
    if (count <= 0 || width <= 0) return null;
    final i = (dx / (width / count)).floor();
    return (i < 0 || i >= count) ? null : i;
  }
}

class _TrendPainter extends CustomPainter {
  _TrendPainter({
    required this.values,
    required this.maxValue,
    required this.average,
    required this.bar,
    required this.axis,
    required this.label,
    required this.textDirection,
  });

  final List<num> values;
  final num maxValue;
  final double average;
  final Color bar;
  final Color axis;
  final Color label;
  final TextDirection textDirection;

  @override
  void paint(Canvas canvas, Size size) {
    if (values.isEmpty) return;
    const labelBand = 16.0; // room for the value label above the tallest bar
    final plotH = math.max(1.0, size.height - labelBand);
    // Headroom so the tallest bar never touches the label band.
    final scaleMax = math.max(1.0, maxValue.toDouble() * 1.12);
    final slot = size.width / values.length;
    final barW = math.max(2.0, slot - 2); // 2px surface gap between bars

    // Baseline + average reference, both recessive.
    final axisPaint = Paint()
      ..color = axis
      ..strokeWidth = 1;
    canvas.drawLine(Offset(0, plotH), Offset(size.width, plotH), axisPaint);
    if (average > 0) {
      final y = plotH - (average / scaleMax) * plotH;
      final dash = Paint()
        ..color = axis
        ..strokeWidth = 1;
      for (var x = 0.0; x < size.width; x += 6) {
        canvas.drawLine(Offset(x, y), Offset(math.min(x + 3, size.width), y), dash);
      }
    }

    final fill = Paint()..color = bar;
    var maxIndex = 0;
    for (var i = 1; i < values.length; i++) {
      if (values[i] > values[maxIndex]) maxIndex = i;
    }
    for (var i = 0; i < values.length; i++) {
      final v = values[i].toDouble();
      if (v <= 0) continue;
      final h = (v / scaleMax) * plotH;
      final x = i * slot + (slot - barW) / 2;
      canvas.drawRRect(
        RRect.fromRectAndCorners(
          Rect.fromLTWH(x, plotH - h, barW, h),
          // 4px rounded data end, anchored square to the baseline.
          topLeft: const Radius.circular(4),
          topRight: const Radius.circular(4),
        ),
        fill,
      );
    }
    // Selective direct labels: the peak and the latest day only.
    _label(canvas, values[maxIndex], maxIndex, slot, plotH, scaleMax, size);
    if (maxIndex != values.length - 1) {
      _label(canvas, values.last, values.length - 1, slot, plotH, scaleMax, size);
    }
  }

  void _label(Canvas canvas, num value, int index, double slot, double plotH,
      double scaleMax, Size size) {
    if (value <= 0) return;
    final tp = TextPainter(
      text: TextSpan(
        text: value.round().toString(),
        style: TextStyle(fontSize: 10, color: label),
      ),
      textDirection: textDirection,
    )..layout();
    final h = (value.toDouble() / scaleMax) * plotH;
    final cx = index * slot + slot / 2 - tp.width / 2;
    final clampedX =
        cx.clamp(0.0, math.max(0.0, size.width - tp.width)).toDouble();
    tp.paint(canvas, Offset(clampedX, math.max(0.0, plotH - h - tp.height - 2)));
  }

  @override
  bool shouldRepaint(_TrendPainter old) =>
      old.values.length != values.length ||
      old.maxValue != maxValue ||
      old.average != average ||
      old.bar != bar ||
      old.axis != axis ||
      old.label != label;
}
