/// Meal editor: create a meal by hand or edit/delete an existing one.
///
/// Every rule lives in ui/meal_edit_logic.dart (parsing, bounds, the
/// analysis merge) — this file is the form around it. Saving an EXISTING
/// meal goes through updateMealFields (keeps the row id, the photo's ledger
/// provenance, and marks corrected=1). A NEW meal's provenance depends on
/// how it arrived: MealSource.appManual (typed numbers, no photo, no hash),
/// MealSource.manualText (describe-a-meal preview, no hash), or
/// MealSource.appManualPhoto WITH the photo's md5 when the user hand-enters
/// a photo the model refused.
library;

import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:flutter/material.dart';

import '../../core/coerce.dart' show normalizeImageHash;
import '../../core/contracts.dart';
import '../format.dart' show formatKcal, isoDate;
import '../meal_edit_logic.dart';
import '../widgets/macro_chart.dart';

class MealEditorScreen extends StatefulWidget {
  const MealEditorScreen({
    super.key,
    required this.dao,
    this.meal,
    this.initialDate,
    this.now,
    this.fromPhoto,
    this.makeThumb,
    this.initialAnalysis,
    this.newMealSource = MealSource.appManual,
  });

  final MealsDao dao;

  /// null = create a new meal.
  final Meal? meal;

  /// Pre-selected date for a new meal (the day the user came from).
  final String? initialDate;

  /// Injectable clock for tests.
  final DateTime Function()? now;

  /// Pre-fill for a NEW meal (describe-by-text preview): the model's
  /// analysis lands in the form so the user can check and correct the
  /// numbers BEFORE anything is written. [meal] stays null, so this is
  /// still an insert, not an in-place edit.
  final Map<String, dynamic>? initialAnalysis;

  /// source column for the inserted row — 'manual_text' for describe-by-text
  /// (server parity, spec §4.6), 'app_manual' for a hand-typed meal.
  final String newMealSource;

  /// Creating a meal FROM a photo the analyzer refused (not-food verdict or
  /// a permanent failure): the saved meal carries the photo's identity, so
  /// the ledger is closed (no re-offer loop) and the row gets its picture.
  /// The user supplies the numbers the model wouldn't.
  final IntakePhoto? fromPhoto;

  /// Thumbnail generator seam (production: makeMealThumb in a compute
  /// isolate); null skips the thumb.
  final Future<Uint8List?> Function(Uint8List original)? makeThumb;

  bool get isNew => meal == null;

  @override
  State<MealEditorScreen> createState() => _MealEditorScreenState();
}

class _MealEditorScreenState extends State<MealEditorScreen> {
  late MealDraft _draft;
  late final TextEditingController _desc;
  late final TextEditingController _cal;
  late final TextEditingController _pro;
  late final TextEditingController _carb;
  late final TextEditingController _fat;
  final List<({TextEditingController name, TextEditingController cal,
      TextEditingController pro, TextEditingController carb,
      TextEditingController fat})> _itemCtrls = [];
  List<String> _errors = const [];
  bool _saving = false;
  final ScrollController _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    final clock = widget.now ?? DateTime.now;
    final meal = widget.meal;
    final photo = widget.fromPhoto;
    final prefill = widget.initialAnalysis;
    _draft = meal != null
        ? MealDraft.fromMeal(meal)
        : (prefill != null
            // Describe-by-text preview: load the model's estimate into the
            // form. The synthetic row exists only to reuse the draft loader;
            // widget.meal stays null, so saving INSERTS.
            ? MealDraft.fromMeal(Meal(
                id: 0,
                date: photo?.capturedAt != null
                    ? isoDate(photo!.capturedAt!)
                    : (widget.initialDate ?? isoDate(clock())),
                time: photo?.capturedAt != null
                    ? formatClock(photo!.capturedAt!)
                    : formatClock(clock()),
                timestamp: clock().toIso8601String(),
                source: widget.newMealSource,
                analysis: prefill,
              ))
            : (MealDraft.blank(clock())
              // A photo's own capture moment beats "now" for a manual log.
              ..dateIso = photo?.capturedAt != null
                  ? isoDate(photo!.capturedAt!)
                  : (widget.initialDate ?? MealDraft.blank(clock()).dateIso)
              ..time = photo?.capturedAt != null
                  ? formatClock(photo!.capturedAt!)
                  : MealDraft.blank(clock()).time));
    _desc = TextEditingController(text: _draft.description);
    _cal = TextEditingController(text: _draft.calories);
    _pro = TextEditingController(text: _draft.protein);
    _carb = TextEditingController(text: _draft.carbs);
    _fat = TextEditingController(text: _draft.fat);
    for (final it in _draft.items) {
      _itemCtrls.add(_controllersFor(it));
    }
    // Live macro chart: rebuild as the user types.
    for (final c in [_cal, _pro, _carb, _fat]) {
      c.addListener(_syncTotals);
    }
  }

  ({TextEditingController name, TextEditingController cal,
      TextEditingController pro, TextEditingController carb,
      TextEditingController fat}) _controllersFor(MealItemDraft it) => (
        name: TextEditingController(text: it.name),
        cal: TextEditingController(text: it.calories),
        pro: TextEditingController(text: it.protein),
        carb: TextEditingController(text: it.carbs),
        fat: TextEditingController(text: it.fat),
      );

  @override
  void dispose() {
    _scroll.dispose();
    for (final c in [_desc, _cal, _pro, _carb, _fat]) {
      c.dispose();
    }
    for (final row in _itemCtrls) {
      row.name.dispose();
      row.cal.dispose();
      row.pro.dispose();
      row.carb.dispose();
      row.fat.dispose();
    }
    super.dispose();
  }

  void _syncTotals() {
    setState(() {
      _draft.calories = _cal.text;
      _draft.protein = _pro.text;
      _draft.carbs = _carb.text;
      _draft.fat = _fat.text;
    });
  }

  void _harvest() {
    _draft.description = _desc.text;
    _draft.calories = _cal.text;
    _draft.protein = _pro.text;
    _draft.carbs = _carb.text;
    _draft.fat = _fat.text;
    for (var i = 0; i < _draft.items.length; i++) {
      final c = _itemCtrls[i];
      _draft.items[i]
        ..name = c.name.text
        ..calories = c.cal.text
        ..protein = c.pro.text
        ..carbs = c.carb.text
        ..fat = c.fat.text;
    }
  }

  Future<void> _save() async {
    if (_saving) return;
    _harvest();
    final errors = _draft.validate();
    if (errors.isNotEmpty) {
      setState(() => _errors = errors);
      // The error card is the FIRST list child while Save is a pinned FAB:
      // on a scrolled form the rejection would be invisible and Save would
      // look like a dead button. Say it where the finger is, then scroll.
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(errors.length == 1
              ? errors.single
              : '${errors.length} things need fixing — see the top.')));
      if (_scroll.hasClients) {
        await _scroll.animateTo(0,
            duration: const Duration(milliseconds: 250),
            curve: Curves.easeOut);
      }
      return;
    }
    setState(() {
      _errors = const [];
      _saving = true;
    });
    try {
      final analysis = _draft.toAnalysis();
      if (widget.isNew) {
        final photo = widget.fromPhoto;
        // md5 of the ORIGINAL bytes: the same identity the watcher uses, so
        // marking the ledger 'saved' stops this photo being re-offered
        // forever (spec §6.2/§2.3).
        final hash = photo == null
            ? ''
            : normalizeImageHash(md5.convert(photo.bytes).toString());
        final id = await widget.dao.saveMeal(
          Meal(
            id: 0, // assigned on insert
            date: _draft.dateIso.trim(),
            time: _draft.time.trim(),
            timestamp: (widget.now ?? DateTime.now)().toIso8601String(),
            // Photo-backed manual logs keep their own source; otherwise the
            // caller decides ('manual_text' for describe-by-text, server
            // parity spec §4.6; 'app_manual' for a hand-typed meal).
            source: photo == null
                ? widget.newMealSource
                : MealSource.appManualPhoto,
            imageHash: hash,
            fileId: photo?.assetId ?? '',
            analysis: analysis,
          ),
          markStatus: photo == null ? null : IngestionStatus.saved,
        );
        if (photo != null && widget.makeThumb != null) {
          try {
            final thumb = await widget.makeThumb!(photo.bytes);
            if (thumb != null) await widget.dao.saveMealThumb(id, thumb);
          } catch (_) {
            // A missing thumb is cosmetic; the meal is already saved.
          }
        }
      } else {
        await widget.dao.updateMealFields(
          widget.meal!.id,
          analysis: analysis,
          date: _draft.dateIso.trim(),
          time: _draft.time.trim(),
        );
      }
      if (!mounted) return;
      Navigator.of(context).pop(true); // changed
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        _errors = ['Could not save: $e'];
      });
    }
  }

  Future<void> _delete() async {
    final meal = widget.meal;
    if (meal == null) return;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete this meal?'),
        content: const Text(
            'It will be removed from your history and totals. This cannot '
            'be undone.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('Cancel')),
          FilledButton(
              key: const Key('confirmDeleteMeal'),
              onPressed: () => Navigator.of(ctx).pop(true),
              child: const Text('Delete')),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    setState(() => _saving = true);
    try {
      await widget.dao.deleteMeal(meal.id);
      if (!mounted) return;
      Navigator.of(context).pop(true);
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        _errors = ['Could not delete: $e'];
      });
    }
  }

  Future<void> _pickDate() async {
    final current = DateTime.tryParse(_draft.dateIso) ?? (widget.now ?? DateTime.now)();
    final picked = await showDatePicker(
      context: context,
      initialDate: current,
      firstDate: DateTime(2020),
      lastDate: (widget.now ?? DateTime.now)().add(const Duration(days: 1)),
    );
    if (picked == null || !mounted) return;
    setState(() => _draft.dateIso =
        '${picked.year.toString().padLeft(4, '0')}-'
        '${picked.month.toString().padLeft(2, '0')}-'
        '${picked.day.toString().padLeft(2, '0')}');
  }

  Future<void> _pickTime() async {
    final parsed = parseClock(_draft.time);
    final picked = await showTimePicker(
      context: context,
      initialTime: TimeOfDay(
          hour: parsed?.hour ?? 12, minute: parsed?.minute ?? 0),
    );
    if (picked == null || !mounted) return;
    setState(() => _draft.time = formatClock(DateTime(
        2026, 1, 1, picked.hour, picked.minute)));
  }

  /// Remove by IDENTITY, never by the index captured at build time: two
  /// taps inside one frame otherwise delete the wrong row (or throw
  /// RangeError on the second).
  void _removeItem(MealItemDraft item) {
    _harvest();
    final i = _draft.items.indexOf(item);
    if (i < 0) return; // already gone (double tap)
    setState(() {
      final row = _itemCtrls.removeAt(i);
      row.name.dispose();
      row.cal.dispose();
      row.pro.dispose();
      row.carb.dispose();
      row.fat.dispose();
      _draft.items.removeAt(i);
    });
  }

  void _useItemTotals() {
    _harvest();
    final t = _draft.itemTotals();
    // Clamp to the same ceilings validate() enforces: writing 39,998 into a
    // field that refuses to save above 20,000 is a trap, not a shortcut.
    final capped = t.calories > maxMealCalories ||
        t.protein > maxMacroGrams ||
        t.carbs > maxMacroGrams ||
        t.fat > maxMacroGrams;
    setState(() {
      _cal.text = _plain(t.calories.clamp(0, maxMealCalories));
      _pro.text = _plain(t.protein.clamp(0, maxMacroGrams));
      _carb.text = _plain(t.carbs.clamp(0, maxMacroGrams));
      _fat.text = _plain(t.fat.clamp(0, maxMacroGrams));
    });
    if (capped) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('Item totals exceeded the maximum and were capped.')));
    }
  }

  static String _plain(num v) =>
      v == v.roundToDouble() ? v.round().toString() : v.toString();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final kcalNow =
        parseNumberField(_cal.text, label: 'Calories', max: maxMealCalories)
                .value ??
            0;
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.isNew ? 'Add meal' : 'Edit meal'),
        actions: [
          if (!widget.isNew)
            IconButton(
              key: const Key('deleteMealButton'),
              tooltip: 'Delete meal',
              icon: const Icon(Icons.delete_outline),
              onPressed: _saving ? null : _delete,
            ),
        ],
      ),
      body: ListView(
        controller: _scroll,
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 96),
        children: [
          if (_errors.isNotEmpty)
            Card(
              key: const Key('editorErrors'),
              color: theme.colorScheme.errorContainer,
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    for (final e in _errors)
                      Text(e,
                          style: TextStyle(
                              color: theme.colorScheme.onErrorContainer)),
                  ],
                ),
              ),
            ),
          TextField(
            key: const Key('editorDescription'),
            controller: _desc,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(
              labelText: 'What was it?',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: OutlinedButton.icon(
                  key: const Key('editorDateButton'),
                  onPressed: _pickDate,
                  icon: const Icon(Icons.event_outlined, size: 18),
                  label: Text(_draft.dateIso),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: OutlinedButton.icon(
                  key: const Key('editorTimeButton'),
                  onPressed: _pickTime,
                  icon: const Icon(Icons.schedule_outlined, size: 18),
                  label: Text(_draft.time),
                ),
              ),
            ],
          ),
          const SizedBox(height: 20),
          Text('Totals', style: theme.textTheme.titleSmall),
          const SizedBox(height: 8),
          _NumberField(
            fieldKey: const Key('editorCalories'),
            controller: _cal,
            label: 'Calories (kcal)',
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: _NumberField(
                    fieldKey: const Key('editorProtein'),
                    controller: _pro,
                    label: 'Protein (g)'),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: _NumberField(
                    fieldKey: const Key('editorCarbs'),
                    controller: _carb,
                    label: 'Carbs (g)'),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: _NumberField(
                    fieldKey: const Key('editorFat'),
                    controller: _fat,
                    label: 'Fat (g)'),
              ),
            ],
          ),
          const SizedBox(height: 16),
          // Live composition of what's typed above.
          Text('~${formatKcal(kcalNow)} kcal',
              style: theme.textTheme.titleMedium),
          const SizedBox(height: 8),
          MacroBreakdownBar(
            proteinG: _num(_pro.text),
            carbsG: _num(_carb.text),
            fatG: _num(_fat.text),
          ),
          const SizedBox(height: 24),
          Row(
            children: [
              Expanded(
                  child:
                      Text('Items', style: theme.textTheme.titleSmall)),
              if (_draft.items.isNotEmpty)
                TextButton(
                  key: const Key('useItemTotals'),
                  onPressed: _useItemTotals,
                  child: const Text('Sum into totals'),
                ),
            ],
          ),
          for (var i = 0; i < _draft.items.length; i++)
            _ItemRow(
              // Identity key: element (and focus) state must travel WITH the
              // item when a row above it is removed. Without it, removing a
              // row left the keyboard bound to the next item's controller
              // and silently typed into the wrong food.
              key: ObjectKey(_draft.items[i]),
              index: i,
              ctrls: _itemCtrls[i],
              onRemove: () => _removeItem(_draft.items[i]),
            ),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              key: const Key('addItemRow'),
              onPressed: () {
                _harvest();
                setState(() {
                  final it = MealItemDraft();
                  _draft.items.add(it);
                  _itemCtrls.add(_controllersFor(it));
                });
              },
              icon: const Icon(Icons.add, size: 18),
              label: const Text('Add item'),
            ),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        key: const Key('saveMealButton'),
        onPressed: _saving ? null : _save,
        icon: _saving
            ? const SizedBox(
                width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
            : const Icon(Icons.check),
        label: Text(widget.isNew ? 'Add' : 'Save'),
      ),
    );
  }

  static num _num(String raw) =>
      parseNumberField(raw, label: 'x', max: maxMacroGrams).value ?? 0;
}

class _NumberField extends StatelessWidget {
  const _NumberField({
    required this.fieldKey,
    required this.controller,
    required this.label,
  });

  final Key fieldKey;
  final TextEditingController controller;
  final String label;

  @override
  Widget build(BuildContext context) => TextField(
        key: fieldKey,
        controller: controller,
        keyboardType: const TextInputType.numberWithOptions(decimal: true),
        decoration: InputDecoration(
          labelText: label,
          border: const OutlineInputBorder(),
          isDense: true,
        ),
      );
}

class _ItemRow extends StatelessWidget {
  const _ItemRow({
    super.key,
    required this.index,
    required this.ctrls,
    required this.onRemove,
  });

  final int index;
  final ({TextEditingController name, TextEditingController cal,
      TextEditingController pro, TextEditingController carb,
      TextEditingController fat}) ctrls;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Column(
          children: [
            Row(
              children: [
                Expanded(
                  child: TextField(
                    key: Key('itemName$index'),
                    controller: ctrls.name,
                    decoration: const InputDecoration(
                      labelText: 'Item',
                      border: OutlineInputBorder(),
                      isDense: true,
                    ),
                  ),
                ),
                IconButton(
                  key: Key('removeItem$index'),
                  tooltip: 'Remove item',
                  icon: const Icon(Icons.close),
                  onPressed: onRemove,
                ),
              ],
            ),
            const SizedBox(height: 6),
            Row(
              children: [
                Expanded(
                    child: _NumberField(
                        fieldKey: Key('itemCal$index'),
                        controller: ctrls.cal,
                        label: 'kcal')),
                const SizedBox(width: 6),
                Expanded(
                    child: _NumberField(
                        fieldKey: Key('itemPro$index'),
                        controller: ctrls.pro,
                        label: 'P g')),
                const SizedBox(width: 6),
                Expanded(
                    child: _NumberField(
                        fieldKey: Key('itemCarb$index'),
                        controller: ctrls.carb,
                        label: 'C g')),
                const SizedBox(width: 6),
                Expanded(
                    child: _NumberField(
                        fieldKey: Key('itemFat$index'),
                        controller: ctrls.fat,
                        label: 'F g')),
              ],
            ),
          ],
        ),
      );
}
