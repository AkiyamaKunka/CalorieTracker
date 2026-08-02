/// The Body tab: weight history + girth measurements (waist / chest / hip).
///
/// Weight rides the parity `body_weight` table (spec §7.1 — one canonical
/// weigh-in per day, which the NL executor has been writing all along with
/// no UI to show for it). Measurements are app-only (spec §9): the server
/// tracks weight, not girth.
///
/// Design rules follow the app's chart idiom (macro_chart.dart): hand-drawn
/// CustomPainter, one hue per series, recessive axes, selective labels
/// (min / max / latest), identity never colour-alone.
library;

import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../../core/shared_generated.dart';
import '../widgets/macro_chart.dart' show MacroPalette;

/// Chart + list window. A year keeps the page honest for lapsed loggers
/// (a 90-day window would silently hide that the last entry is old).
const int kBodyWindowDays = 365;

/// App-only sanity bounds for girth values (cm). Weight uses the shared
/// parity bounds [SharedConstants.weightMinKg]/[SharedConstants.weightMaxKg].
const double kGirthMinCm = 20;
const double kGirthMaxCm = 300;

class BodyScreen extends StatefulWidget {
  const BodyScreen({super.key, required this.dao, this.clock});
  final MealsDao dao;

  /// Test seam; defaults to [DateTime.now].
  final DateTime Function()? clock;

  @override
  State<BodyScreen> createState() => BodyScreenState();
}

class BodyScreenState extends State<BodyScreen> {
  List<WeightEntry> _weights = const [];
  List<BodyMeasurements> _measurements = const [];
  bool _loading = true;

  DateTime get _now => (widget.clock ?? DateTime.now)();

  static String _iso(DateTime d) =>
      '${d.year.toString().padLeft(4, '0')}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';

  @override
  void initState() {
    super.initState();
    reload();
  }

  /// Re-query both tables. Public: the shell calls this on tab selection,
  /// same as the other data tabs (IndexedStack keeps stale state alive).
  Future<void> reload() async {
    final now = _now;
    final start = _iso(now.subtract(const Duration(days: kBodyWindowDays)));
    final end = _iso(now);
    final weights = await widget.dao.listBodyWeights(start, end);
    final measurements = await widget.dao.listBodyMeasurements(start, end);
    if (!mounted) return;
    setState(() {
      _weights = weights;
      _measurements = measurements;
      _loading = false;
    });
  }

  /// All dates carrying any data, newest first, for the history list.
  List<String> get _dates {
    final set = <String>{
      for (final w in _weights) w.date,
      for (final m in _measurements) m.date,
    };
    final list = set.toList()..sort((a, b) => b.compareTo(a));
    return list;
  }

  WeightEntry? _weightOn(String date) {
    for (final w in _weights) {
      if (w.date == date) return w;
    }
    return null;
  }

  BodyMeasurements? _measurementsOn(String date) {
    for (final m in _measurements) {
      if (m.date == date) return m;
    }
    return null;
  }

  Future<void> _openSheet({String? date}) async {
    final initialDate = date ?? _iso(_now);
    final saved = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      builder: (_) => LogBodySheet(
        dao: widget.dao,
        date: initialDate,
        existingWeight: _weightOn(initialDate)?.kg,
        existing: _measurementsOn(initialDate),
        latestDate: date == null ? null : _iso(_now),
      ),
    );
    if (saved == true) await reload();
  }

  Future<void> _confirmDelete(String date) async {
    final hasWeight = _weightOn(date) != null;
    final hasMeasurements = _measurementsOn(date) != null;
    final what = [
      if (hasWeight) 'weight',
      if (hasMeasurements) 'measurements',
    ].join(' and ');
    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Delete $date?'),
        content: Text('Removes the $what recorded for this day.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          FilledButton(
              key: const Key('confirmDeleteBodyDay'),
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Delete')),
        ],
      ),
    );
    if (ok != true) return;
    if (hasWeight) await widget.dao.deleteBodyWeight(date);
    if (hasMeasurements) await widget.dao.deleteBodyMeasurements(date);
    await reload();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    final empty = _weights.isEmpty && _measurements.isEmpty;
    return Scaffold(
      floatingActionButton: FloatingActionButton.extended(
        key: const Key('logBodyButton'),
        // The IndexedStack keeps Today's FAB alive at the same time; the
        // default shared hero tag makes route animations throw.
        heroTag: 'bodyLogFab',
        onPressed: () => _openSheet(),
        icon: const Icon(Icons.add),
        label: const Text('Log'),
      ),
      body: empty
          ? Center(
              child: Padding(
                padding: const EdgeInsets.all(32),
                child: Column(
                  key: const Key('bodyEmpty'),
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.monitor_weight_outlined,
                        size: 48, color: theme.colorScheme.onSurfaceVariant),
                    const SizedBox(height: 12),
                    Text('No body data yet.',
                        style: theme.textTheme.titleMedium),
                    const SizedBox(height: 8),
                    Text(
                      'Tap Log to record your weight or your waist, chest '
                      'and hip measurements. Weight logged by chat ("I '
                      'weigh 81.6 kg") lands here too.',
                      textAlign: TextAlign.center,
                      style: theme.textTheme.bodyMedium?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant),
                    ),
                  ],
                ),
              ),
            )
          : ListView(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 96),
              children: [
                if (_weights.isNotEmpty) _WeightCard(weights: _weights),
                if (_measurements.isNotEmpty) ...[
                  const SizedBox(height: 12),
                  _MeasurementsCard(measurements: _measurements),
                ],
                const SizedBox(height: 20),
                Text('History', style: theme.textTheme.titleMedium),
                const SizedBox(height: 4),
                for (final date in _dates)
                  _HistoryRow(
                    key: Key('bodyDay$date'),
                    date: date,
                    weight: _weightOn(date),
                    measurements: _measurementsOn(date),
                    onTap: () => _openSheet(date: date),
                    onDelete: () => _confirmDelete(date),
                  ),
              ],
            ),
    );
  }
}

// ─── weight card ────────────────────────────────────────────────────

class _WeightCard extends StatelessWidget {
  const _WeightCard({required this.weights});
  final List<WeightEntry> weights; // ascending

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final latest = weights.last;
    final previous = weights.length > 1 ? weights[weights.length - 2] : null;
    final delta = previous == null ? null : latest.kg - previous.kg;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Weight', style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Text(_kg(latest.kg),
                    key: const Key('bodyWeightHeadline'),
                    style: theme.textTheme.displaySmall),
                const SizedBox(width: 4),
                Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: Text('kg', style: theme.textTheme.titleMedium),
                ),
                const Spacer(),
                if (delta != null)
                  _DeltaChip(
                      key: const Key('bodyWeightDelta'),
                      delta: delta,
                      unit: 'kg',
                      sinceDate: previous!.date),
              ],
            ),
            Text('on ${latest.date}',
                style: theme.textTheme.bodySmall
                    ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
            if (weights.length >= 2) ...[
              const SizedBox(height: 16),
              SizedBox(
                height: 140,
                width: double.infinity,
                child: CustomPaint(
                  key: const Key('weightTrendChart'),
                  painter: WeightTrendPainter(
                    entries: weights,
                    line: MacroPalette.of(context).protein,
                    axis: theme.colorScheme.outlineVariant,
                    label: theme.colorScheme.onSurfaceVariant,
                    textDirection: Directionality.of(context),
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  static String _kg(double v) =>
      v == v.roundToDouble() ? v.round().toString() : v.toStringAsFixed(1);
}

class _DeltaChip extends StatelessWidget {
  const _DeltaChip(
      {super.key,
      required this.delta,
      required this.unit,
      required this.sinceDate});
  final double delta;
  final String unit;
  final String sinceDate;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    // Direction glyph + sign carried in TEXT (never colour-alone); colour is
    // deliberately neutral — for weight, neither direction is universally
    // "good", so green/red would editorialize.
    final sign = delta > 0 ? '▲ +' : (delta < 0 ? '▼ ' : '· ');
    final text = delta == 0
        ? 'no change'
        : '$sign${delta.abs().toStringAsFixed(1)} $unit';
    return Tooltip(
      message: 'since $sinceDate',
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        decoration: BoxDecoration(
          color: theme.colorScheme.surfaceContainerHighest,
          borderRadius: BorderRadius.circular(12),
        ),
        child: Text(text, style: theme.textTheme.labelLarge),
      ),
    );
  }
}

// ─── measurements card ──────────────────────────────────────────────

class _MeasurementsCard extends StatelessWidget {
  const _MeasurementsCard({required this.measurements});
  final List<BodyMeasurements> measurements; // ascending

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Measurements', style: theme.textTheme.titleMedium),
            const SizedBox(height: 4),
            _metricRow(context, 'Waist', (m) => m.waistCm,
                const Key('bodyWaistLatest')),
            _metricRow(context, 'Chest', (m) => m.chestCm,
                const Key('bodyChestLatest')),
            _metricRow(
                context, 'Hip', (m) => m.hipCm, const Key('bodyHipLatest')),
          ],
        ),
      ),
    );
  }

  Widget _metricRow(BuildContext context, String label,
      double? Function(BodyMeasurements) pick, Key key) {
    final theme = Theme.of(context);
    // Latest + previous NON-NULL values for this metric — days that logged
    // only the other metrics must not break a series.
    final series = [
      for (final m in measurements)
        if (pick(m) != null) (m.date, pick(m)!),
    ];
    if (series.isEmpty) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Row(
          children: [
            SizedBox(width: 64, child: Text(label)),
            Text('—',
                style: TextStyle(color: theme.colorScheme.onSurfaceVariant)),
          ],
        ),
      );
    }
    final latest = series.last;
    final previous = series.length > 1 ? series[series.length - 2] : null;
    final delta = previous == null ? null : latest.$2 - previous.$2;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: [
          SizedBox(width: 64, child: Text(label)),
          Text('${_cm(latest.$2)} cm',
              key: key, style: theme.textTheme.titleMedium),
          const SizedBox(width: 8),
          Text('on ${latest.$1}',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant)),
          const Spacer(),
          if (delta != null && delta != 0)
            Text(
              '${delta > 0 ? '▲ +' : '▼ '}${delta.abs().toStringAsFixed(1)}',
              style: theme.textTheme.labelMedium
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
        ],
      ),
    );
  }

  static String _cm(double v) =>
      v == v.roundToDouble() ? v.round().toString() : v.toStringAsFixed(1);
}

// ─── history row ────────────────────────────────────────────────────

class _HistoryRow extends StatelessWidget {
  const _HistoryRow({
    super.key,
    required this.date,
    required this.weight,
    required this.measurements,
    required this.onTap,
    required this.onDelete,
  });
  final String date;
  final WeightEntry? weight;
  final BodyMeasurements? measurements;
  final VoidCallback onTap;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    final parts = <String>[
      if (weight != null) '${weight!.kg.toStringAsFixed(1)} kg',
      if (measurements?.waistCm != null) 'W ${_v(measurements!.waistCm!)}',
      if (measurements?.chestCm != null) 'C ${_v(measurements!.chestCm!)}',
      if (measurements?.hipCm != null) 'H ${_v(measurements!.hipCm!)}',
    ];
    return ListTile(
      contentPadding: const EdgeInsets.only(left: 8, right: 0),
      title: Text(date),
      subtitle: Text(parts.join('  ·  ')),
      onTap: onTap,
      trailing: IconButton(
        icon: const Icon(Icons.delete_outline),
        tooltip: 'Delete $date',
        onPressed: onDelete,
      ),
    );
  }

  static String _v(double v) =>
      v == v.roundToDouble() ? v.round().toString() : v.toStringAsFixed(1);
}

// ─── log sheet ──────────────────────────────────────────────────────

/// Bottom sheet for logging or editing one day. Prefilled with the day's
/// existing values, so the DAO's full-row upsert preserves what the user
/// didn't touch — and clearing a field really clears it.
class LogBodySheet extends StatefulWidget {
  const LogBodySheet({
    super.key,
    required this.dao,
    required this.date,
    this.existingWeight,
    this.existing,
    this.latestDate,
  });
  final MealsDao dao;
  final String date;
  final double? existingWeight;
  final BodyMeasurements? existing;

  /// When editing an old day, today's date (for the header hint); null when
  /// already logging today.
  final String? latestDate;

  @override
  State<LogBodySheet> createState() => _LogBodySheetState();
}

class _LogBodySheetState extends State<LogBodySheet> {
  late final TextEditingController _weight;
  late final TextEditingController _waist;
  late final TextEditingController _chest;
  late final TextEditingController _hip;
  String? _error;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _weight = TextEditingController(text: _fmt(widget.existingWeight));
    _waist = TextEditingController(text: _fmt(widget.existing?.waistCm));
    _chest = TextEditingController(text: _fmt(widget.existing?.chestCm));
    _hip = TextEditingController(text: _fmt(widget.existing?.hipCm));
  }

  static String _fmt(double? v) => v == null
      ? ''
      : (v == v.roundToDouble() ? v.round().toString() : v.toStringAsFixed(1));

  @override
  void dispose() {
    _weight.dispose();
    _waist.dispose();
    _chest.dispose();
    _hip.dispose();
    super.dispose();
  }

  /// Parse one field: (value, problem). Empty is a valid "no value".
  (double?, String?) _parse(
      TextEditingController c, String label, double min, double max) {
    final raw = c.text.trim().replaceAll(',', '.');
    if (raw.isEmpty) return (null, null);
    final v = double.tryParse(raw);
    if (v == null) return (null, '$label: "$raw" is not a number.');
    if (v < min || v > max) {
      return (null, '$label must be between ${_num(min)} and ${_num(max)}.');
    }
    return (v, null);
  }

  static String _num(double v) =>
      v == v.roundToDouble() ? v.round().toString() : v.toString();

  Future<void> _save() async {
    final (kg, e1) = _parse(_weight, 'Weight',
        SharedConstants.weightMinKg.toDouble(),
        SharedConstants.weightMaxKg.toDouble());
    final (waist, e2) = _parse(_waist, 'Waist', kGirthMinCm, kGirthMaxCm);
    final (chest, e3) = _parse(_chest, 'Chest', kGirthMinCm, kGirthMaxCm);
    final (hip, e4) = _parse(_hip, 'Hip', kGirthMinCm, kGirthMaxCm);
    final problem = e1 ?? e2 ?? e3 ?? e4;
    if (problem != null) {
      setState(() => _error = problem);
      return;
    }
    final hadAnything = widget.existingWeight != null ||
        (widget.existing != null && !widget.existing!.isEmpty);
    if (kg == null && waist == null && chest == null && hip == null &&
        !hadAnything) {
      setState(() => _error = 'Enter at least one value.');
      return;
    }
    setState(() {
      _saving = true;
      _error = null;
    });
    // Weight: save or (when this edit cleared a previously present value)
    // delete — leaving the stale row would resurrect it on next load.
    if (kg != null) {
      await widget.dao.saveBodyWeight(widget.date, kg, source: 'manual');
    } else if (widget.existingWeight != null) {
      await widget.dao.deleteBodyWeight(widget.date);
    }
    // Measurements: full-row upsert; all-empty deletes (DAO contract).
    if (waist != null || chest != null || hip != null ||
        widget.existing != null) {
      await widget.dao.saveBodyMeasurements(widget.date,
          waistCm: waist, chestCm: chest, hipCm: hip);
    }
    if (mounted) Navigator.pop(context, true);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      // Keep the sheet above the keyboard.
      padding: EdgeInsets.only(
          bottom: MediaQuery.of(context).viewInsets.bottom),
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              widget.latestDate == null
                  ? 'Log body · ${widget.date}'
                  : 'Edit ${widget.date}',
              style: theme.textTheme.titleLarge,
            ),
            const SizedBox(height: 4),
            Text(
              'Leave anything you did not measure empty.',
              style: theme.textTheme.bodySmall
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    key: const Key('bodyWeightField'),
                    controller: _weight,
                    keyboardType: const TextInputType.numberWithOptions(
                        decimal: true),
                    decoration: const InputDecoration(
                      labelText: 'Weight',
                      suffixText: 'kg',
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: TextField(
                    key: const Key('bodyWaistField'),
                    controller: _waist,
                    keyboardType: const TextInputType.numberWithOptions(
                        decimal: true),
                    decoration: const InputDecoration(
                      labelText: 'Waist',
                      suffixText: 'cm',
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    key: const Key('bodyChestField'),
                    controller: _chest,
                    keyboardType: const TextInputType.numberWithOptions(
                        decimal: true),
                    decoration: const InputDecoration(
                      labelText: 'Chest',
                      suffixText: 'cm',
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: TextField(
                    key: const Key('bodyHipField'),
                    controller: _hip,
                    keyboardType: const TextInputType.numberWithOptions(
                        decimal: true),
                    decoration: const InputDecoration(
                      labelText: 'Hip',
                      suffixText: 'cm',
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
              ],
            ),
            if (_error != null) ...[
              const SizedBox(height: 12),
              Text(_error!,
                  key: const Key('bodySheetError'),
                  style: TextStyle(color: theme.colorScheme.error)),
            ],
            const SizedBox(height: 16),
            SizedBox(
              width: double.infinity,
              child: FilledButton(
                key: const Key('bodySheetSave'),
                onPressed: _saving ? null : _save,
                child: Text(_saving ? 'Saving…' : 'Save'),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ─── weight trend painter ───────────────────────────────────────────

/// Weight over time: a line through date-proportional x positions (a gap in
/// logging shows as a long segment, not a lie of continuity), dots on the
/// data, selective labels on min / max / latest.
class WeightTrendPainter extends CustomPainter {
  WeightTrendPainter({
    required this.entries,
    required this.line,
    required this.axis,
    required this.label,
    required this.textDirection,
  });

  final List<WeightEntry> entries; // ascending by date
  final Color line;
  final Color axis;
  final Color label;
  final TextDirection textDirection;

  @override
  void paint(Canvas canvas, Size size) {
    if (entries.length < 2) return;
    const pad = 14.0; // label headroom top + bottom
    final plot = Rect.fromLTWH(0, pad, size.width, size.height - 2 * pad);

    final days = [for (final e in entries) _dayNumber(e.date)];
    final minDay = days.first, maxDay = days.last;
    final span = math.max(1, maxDay - minDay);
    final values = [for (final e in entries) e.kg];
    var minV = values.reduce(math.min), maxV = values.reduce(math.max);
    // Half-kilo floor on the domain: a flat week must read flat, not as
    // dramatic swings across a 0.1 kg range.
    if (maxV - minV < 1.0) {
      final mid = (maxV + minV) / 2;
      minV = mid - 0.5;
      maxV = mid + 0.5;
    }

    Offset at(int i) {
      final x = plot.left + (days[i] - minDay) / span * plot.width;
      final y = plot.bottom -
          (values[i] - minV) / (maxV - minV) * plot.height;
      return Offset(x, y);
    }

    // Recessive baseline.
    canvas.drawLine(Offset(plot.left, plot.bottom),
        Offset(plot.right, plot.bottom), Paint()..color = axis..strokeWidth = 1);

    final path = Path()..moveTo(at(0).dx, at(0).dy);
    for (var i = 1; i < entries.length; i++) {
      path.lineTo(at(i).dx, at(i).dy);
    }
    canvas.drawPath(
        path,
        Paint()
          ..color = line
          ..style = PaintingStyle.stroke
          ..strokeWidth = 2
          ..strokeJoin = StrokeJoin.round);
    final dot = Paint()..color = line;
    for (var i = 0; i < entries.length; i++) {
      canvas.drawCircle(at(i), 3, dot);
    }

    // Selective labels: min, max, latest (dedup indices).
    var minI = 0, maxI = 0;
    for (var i = 1; i < values.length; i++) {
      if (values[i] < values[minI]) minI = i;
      if (values[i] > values[maxI]) maxI = i;
    }
    for (final i in {minI, maxI, values.length - 1}) {
      final tp = TextPainter(
        text: TextSpan(
            text: values[i].toStringAsFixed(1),
            style: TextStyle(fontSize: 10, color: label)),
        textDirection: textDirection,
      )..layout();
      final p = at(i);
      final above = i == maxI; // max labels above the dot, others below
      final x =
          p.dx.clamp(0.0, math.max(0.0, size.width - tp.width)).toDouble();
      final y = (above ? p.dy - tp.height - 5 : p.dy + 5)
          .clamp(0.0, size.height - tp.height)
          .toDouble();
      tp.paint(canvas, Offset(x, y));
    }
  }

  /// Days since epoch for YYYY-MM-DD (UTC — only differences matter).
  static int _dayNumber(String date) =>
      DateTime.parse('${date}T00:00:00Z').millisecondsSinceEpoch ~/
      Duration.millisecondsPerDay;

  @override
  bool shouldRepaint(WeightTrendPainter old) =>
      old.entries.length != entries.length ||
      old.line != line ||
      (entries.isNotEmpty &&
          old.entries.isNotEmpty &&
          (old.entries.last.date != entries.last.date ||
              old.entries.last.kg != entries.last.kg));
}
