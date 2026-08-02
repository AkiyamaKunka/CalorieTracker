/// The shareable day report — one tall image ("长图") of a day's intake,
/// built for the report-to-my-coach flow (user request 2026-08-02).
///
/// Two halves:
///  * [DayReportCard] — a SYNCHRONOUSLY-painting widget (thumbnails arrive
///    as pre-decoded [ui.Image]s drawn with RawImage; no FutureBuilder, no
///    ImageProvider async) so a single offscreen frame contains everything.
///  * [renderDayReport] — lays the card out in its own detached render
///    tree (the captureFromWidget technique: RenderView + BuildOwner +
///    RenderRepaintBoundary, never touching the live tree), rasterizes at
///    [kReportPixelRatio] and returns PNG bytes.
///
/// Design: the app's own grouped-Apple language on the neutral grouped
/// background — a coach receives the same visual truth the user sees,
/// including the transparent Typical/Burn/Eaten arithmetic and the
/// portion-size assumptions in item names.
library;

import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import '../../core/coerce.dart';
import '../../core/contracts.dart';
import '../format.dart';
import '../l10n.dart';
import 'calorie_ring.dart';
import 'grouped.dart' show cellBackground, groupedBackground;
import 'macro_chart.dart' show MacroPalette;

/// Logical width of the report; WeChat previews ~3:4 to very tall images
/// fine, and 420 keeps 17pt-class text comfortably readable at 3x.
const double kReportWidth = 420;
const double kReportPixelRatio = 3;

/// One meal, resolved for synchronous painting.
class ReportMeal {
  const ReportMeal({required this.meal, this.thumb});
  final Meal meal;
  final ui.Image? thumb; // pre-decoded; null = no photo block
}

class DayReportCard extends StatelessWidget {
  const DayReportCard({
    super.key,
    required this.date,
    required this.meals,
    this.typicalKcal,
    this.burnKcal = 0,
  });

  final String date; // YYYY-MM-DD
  final List<ReportMeal> meals;
  final num? typicalKcal;
  final num burnKcal;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final palette = MacroPalette.of(context);
    final l = context.l10n;
    final foodMeals =
        meals.where((m) => isFoodMeal(m.meal)).toList(growable: false);
    final totals = todayTotals([for (final m in foodMeals) m.meal]);
    final burn = burnKcal.round();
    return Container(
      width: kReportWidth,
      color: groupedBackground(scheme),
      padding: const EdgeInsets.fromLTRB(16, 24, 16, 18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Header: what this is, and when.
          Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(l.reportTitle,
                        style: theme.textTheme.titleLarge
                            ?.copyWith(fontWeight: FontWeight.w700)),
                    Text(friendlyHistoryDay(date),
                        style: theme.textTheme.bodyMedium
                            ?.copyWith(color: scheme.onSurfaceVariant)),
                  ],
                ),
              ),
              Text(l.kcalAmount(formatKcal(totals.cal)),
                  style: theme.textTheme.headlineSmall),
            ],
          ),
          const SizedBox(height: 14),
          // The hero: same ring + reconciling arithmetic the app shows.
          _cell(
            scheme,
            Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(children: [
                    CalorieRing(
                        eatenKcal: totals.cal,
                        typicalKcal: typicalKcal,
                        burnKcal: burn,
                        size: 116),
                    const SizedBox(width: 16),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(l.reportMeals(totals.meals),
                              style: theme.textTheme.bodySmall?.copyWith(
                                  color: scheme.onSurfaceVariant)),
                          const SizedBox(height: 6),
                          if (typicalKcal != null) ...[
                            ArithmeticRow(
                                label: l.rowTypical,
                                value: '~${formatKcal(typicalKcal!)}',
                                dot: scheme.outlineVariant),
                            if (burn > 0)
                              ArithmeticRow(
                                  label: l.rowBurn,
                                  value: '+${formatKcal(burn)}',
                                  dot: scheme.tertiary),
                            ArithmeticRow(
                                label: l.rowEaten,
                                value: '−${formatKcal(totals.cal.round())}',
                                dot: scheme.primary),
                          ] else
                            ArithmeticRow(
                                label: l.rowEaten,
                                value: formatKcal(totals.cal.round()),
                                dot: scheme.primary),
                        ],
                      ),
                    ),
                  ]),
                  const SizedBox(height: 12),
                  MacroTrio(
                    proteinG: totals.protein,
                    carbsG: totals.carbs,
                    fatG: totals.fat,
                    proteinColor: palette.protein,
                    carbsColor: palette.carbs,
                    fatColor: palette.fat,
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 12),
          // Every meal, in clock order, with its evidence.
          for (final rm in foodMeals) ...[
            _mealBlock(context, rm),
            const SizedBox(height: 10),
          ],
          if (foodMeals.isEmpty)
            _cell(
              scheme,
              Padding(
                padding: const EdgeInsets.all(20),
                child: Text(l.reportNoMeals,
                    style: theme.textTheme.bodyMedium
                        ?.copyWith(color: scheme.onSurfaceVariant)),
              ),
            ),
          const SizedBox(height: 6),
          Center(
            child: Text(l.reportFooter,
                style: theme.textTheme.labelSmall
                    ?.copyWith(color: scheme.outline)),
          ),
        ],
      ),
    );
  }

  Widget _cell(ColorScheme scheme, Widget child) => Material(
        color: cellBackground(scheme),
        borderRadius: BorderRadius.circular(18),
        clipBehavior: Clip.antiAlias,
        child: child,
      );

  Widget _mealBlock(BuildContext context, ReportMeal rm) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final l = context.l10n;
    final a = rm.meal.analysis;
    final items = safeFoodItems(a);
    return _cell(
      scheme,
      Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (rm.thumb != null) ...[
                  ClipRRect(
                    borderRadius: BorderRadius.circular(10),
                    child: RawImage(
                        image: rm.thumb,
                        width: 52,
                        height: 52,
                        fit: BoxFit.cover),
                  ),
                  const SizedBox(width: 10),
                ],
                Expanded(
                  child: Text(mealDescription(a),
                      style: theme.textTheme.titleSmall
                          ?.copyWith(fontWeight: FontWeight.w600)),
                ),
                const SizedBox(width: 8),
                Text(rm.meal.time, style: theme.textTheme.bodySmall),
              ],
            ),
            const SizedBox(height: 6),
            Text(
              '~${l.kcalAmount(displayTotalCalories(a))} · '
              'P ${displayMacro(a, 'total_protein_g')}g · '
              'C ${displayMacro(a, 'total_carbs_g')}g · '
              'F ${displayMacro(a, 'total_fat_g')}g',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: scheme.onSurfaceVariant),
            ),
            if (items.isNotEmpty) ...[
              const SizedBox(height: 6),
              for (final item in items)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(
                    '• ${item['name'] ?? '?'}: '
                    '~${l.kcalAmount(displayItemCalories(item['estimated_calories']))}',
                    style: theme.textTheme.bodySmall,
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }
}

/// Rasterize [DayReportCard] in a detached render tree → PNG bytes.
///
/// The captureFromWidget technique: everything paints in ONE frame because
/// the card is synchronous by construction. Throws on failure — callers
/// surface a snackbar, never a silent nothing.
///
/// [locale] localizes the report's own labels: the detached tree has no
/// ancestor Localizations scope, so without the explicit wrapper the card
/// would silently render in fallback English even for a zh user.
Future<Uint8List> renderDayReport({
  required String date,
  required List<ReportMeal> meals,
  num? typicalKcal,
  num burnKcal = 0,
  ThemeData? theme,
  Locale locale = const Locale('en'),
}) async {
  final boundary = RenderRepaintBoundary();
  final pipelineOwner = PipelineOwner();
  final buildOwner = BuildOwner(focusManager: FocusManager());

  final renderView = RenderView(
    view: WidgetsBinding.instance.platformDispatcher.views.first,
    configuration: ViewConfiguration(
      logicalConstraints: const BoxConstraints(
          minWidth: kReportWidth,
          maxWidth: kReportWidth,
          minHeight: 0,
          maxHeight: double.infinity),
      devicePixelRatio: kReportPixelRatio,
    ),
    child: RenderPositionedBox(
        alignment: Alignment.topCenter, child: boundary),
  );
  pipelineOwner.rootNode = renderView;
  renderView.prepareInitialFrame();

  final element = RenderObjectToWidgetAdapter<RenderBox>(
    container: boundary,
    child: MediaQuery(
      data: const MediaQueryData(
          size: Size(kReportWidth, 2000),
          devicePixelRatio: kReportPixelRatio),
      child: Localizations(
        locale: locale,
        delegates: const [
          AppLocalizations.delegate,
          GlobalMaterialLocalizations.delegate,
          GlobalWidgetsLocalizations.delegate,
          GlobalCupertinoLocalizations.delegate,
        ],
        child: Theme(
          data: theme ?? ThemeData(colorScheme: ColorScheme.fromSeed(
              seedColor: const Color(0xFFCC785C))),
          child: DayReportCard(
              date: date,
              meals: meals,
              typicalKcal: typicalKcal,
              burnKcal: burnKcal),
        ),
      ),
    ),
  ).attachToRenderTree(buildOwner);

  buildOwner
    ..buildScope(element)
    ..finalizeTree();
  pipelineOwner
    ..flushLayout()
    ..flushCompositingBits()
    ..flushPaint();

  final image = await boundary.toImage(pixelRatio: kReportPixelRatio);
  try {
    final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
    if (bytes == null) {
      throw StateError('day report: PNG encoding returned null');
    }
    return bytes.buffer.asUint8List();
  } finally {
    image.dispose();
  }
}

/// Decode stored thumbnail bytes for synchronous painting.
Future<ui.Image?> decodeThumb(Uint8List? bytes) async {
  if (bytes == null || bytes.isEmpty) return null;
  try {
    final codec = await ui.instantiateImageCodec(bytes);
    final frame = await codec.getNextFrame();
    return frame.image;
  } catch (_) {
    return null; // a corrupt thumb must not sink the whole report
  }
}
