/// Unit system for BODY data (weight, girth) — display/input only.
///
/// STORAGE IS ALWAYS METRIC: `body_weight.kg` rides the parity table the
/// server writes, and the shared 30–300 kg bounds are validated in kg —
/// imperial exists purely between the user's eyes/fingers and this layer.
///
/// Resolution rule (user decision 2026-08-03): the Chinese UI is ALWAYS
/// metric; the units setting only takes effect (and is only offered) in
/// English. So a zh device never sees 磅, and the stored preference
/// survives a language round-trip.
library;

import 'package:flutter/widgets.dart';

import 'services.dart' show SettingsStore;

const double _kgPerLb = 0.45359237; // exact, by definition
const double _cmPerIn = 2.54; // exact, by definition

double kgToLb(double kg) => kg / _kgPerLb;
double lbToKg(double lb) => lb * _kgPerLb;
double cmToIn(double cm) => cm / _cmPerIn;
double inToCm(double inches) => inches * _cmPerIn;

/// Whether BODY values should render imperial in this build context.
bool useImperial(BuildContext context, SettingsStore settings) =>
    settings.units == 'imperial' &&
    Localizations.maybeLocaleOf(context)?.languageCode != 'zh';

String weightUnitLabel(bool imperial) => imperial ? 'lb' : 'kg';
String girthUnitLabel(bool imperial) => imperial ? 'in' : 'cm';

/// Stored kg → display number in the active unit (no unit suffix).
double weightForDisplay(double kg, bool imperial) =>
    imperial ? kgToLb(kg) : kg;

/// Stored cm → display number in the active unit (no unit suffix).
double girthForDisplay(double cm, bool imperial) =>
    imperial ? cmToIn(cm) : cm;

/// Typed display-unit value → stored metric.
double weightFromInput(double value, bool imperial) =>
    imperial ? lbToKg(value) : value;

double girthFromInput(double value, bool imperial) =>
    imperial ? inToCm(value) : value;

/// Whole numbers bare, otherwise one decimal — the Body screen's format.
String fmtBodyValue(double v) =>
    v == v.roundToDouble() ? v.round().toString() : v.toStringAsFixed(1);
