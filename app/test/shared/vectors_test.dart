/// Golden-vector replay: shared/vectors/*.json are generated from the Python
/// reference implementation; every case must pass through the Dart ports
/// byte-for-byte. A mismatch is a REAL port bug — fix the Dart side, never
/// the vectors.
///
/// Encoding (documented in each file's header): inputs/outputs are plain
/// JSON except {"__special__": "nan"|"inf"|"-inf"} for non-JSON floats, and
/// an expected of {"__raises__": true} meaning the call throws.
library;

import 'dart:convert';
import 'dart:io';

import 'package:calorie_tracker/core/coerce.dart';
import 'package:calorie_tracker/core/shared_generated.dart';
import 'package:calorie_tracker/services/nl/executor.dart';
import 'package:calorie_tracker/services/photo/filename_dates.dart';
// builders also exports a mealCalorieMismatch; the mismatch vectors pin the
// EXECUTOR's copy (the task contract), so hide the builders one.
import 'package:calorie_tracker/services/report/builders.dart'
    hide mealCalorieMismatch;
import 'package:flutter_test/flutter_test.dart';

/// Locate shared/vectors by walking up from the working directory (tests run
/// from app/, so the first hit is normally ../shared/vectors).
Directory vectorsDir() {
  var dir = Directory.current.absolute;
  while (true) {
    final candidate = Directory('${dir.path}/shared/vectors');
    if (candidate.existsSync()) return candidate;
    final parent = dir.parent;
    if (parent.path == dir.path) {
      throw StateError(
          'shared/vectors not found above ${Directory.current.path}');
    }
    dir = parent;
  }
}

Map<String, dynamic> loadVectors(String name) {
  final file = File('${vectorsDir().path}/$name.json');
  return (jsonDecode(file.readAsStringSync()) as Map).cast<String, dynamic>();
}

/// Decode the {"__special__": ...} float encoding, recursively.
dynamic decodeSpecials(dynamic v) {
  if (v is Map) {
    if (v.length == 1 && v['__special__'] is String) {
      switch (v['__special__']) {
        case 'nan':
          return double.nan;
        case 'inf':
          return double.infinity;
        case '-inf':
          return double.negativeInfinity;
      }
    }
    return <String, dynamic>{
      for (final e in v.entries) '${e.key}': decodeSpecials(e.value),
    };
  }
  if (v is List) return v.map(decodeSpecials).toList();
  return v;
}

bool expectsRaise(dynamic expected) =>
    expected is Map && expected['__raises__'] == true;

/// Deep structural equality with Python-style numeric comparison
/// (500 == 500.0; NaN equals NaN so decoded specials can round-trip).
bool deepEq(dynamic a, dynamic b) {
  if (a is num && b is num) {
    if (a is double && b is double && a.isNaN && b.isNaN) return true;
    return a == b;
  }
  if (a is Map && b is Map) {
    if (a.length != b.length) return false;
    for (final k in a.keys) {
      if (!b.containsKey(k) || !deepEq(a[k], b[k])) return false;
    }
    return true;
  }
  if (a is List && b is List) {
    if (a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (!deepEq(a[i], b[i])) return false;
    }
    return true;
  }
  return a == b;
}

void checkDeepEq(dynamic actual, dynamic expected, String id) {
  expect(deepEq(actual, expected), isTrue,
      reason: '[$id] expected $expected, got $actual');
}

String _two(int n) => n.toString().padLeft(2, '0');

/// Python str(datetime) parity for the captured_at vectors.
String fmtDateTime(DateTime d) =>
    '${d.year.toString().padLeft(4, '0')}-${_two(d.month)}-${_two(d.day)} '
    '${_two(d.hour)}:${_two(d.minute)}:${_two(d.second)}';

void main() {
  group('coerce.json', () {
    final doc = loadVectors('coerce');
    for (final raw in (doc['cases'] as List).cast<Map>()) {
      final c = raw.cast<String, dynamic>();
      final id = '${c['id']}';
      test(id, () {
        final input = decodeSpecials(c['input']);
        final expected = c['expected'];
        switch (c['fn']) {
          case 'safe_number':
            // The vectors pin the nullable-default Python signature; the
            // common non-null shape is safeNumber(), a thin wrapper.
            final fallback = c.containsKey('default')
                ? decodeSpecials(c['default']) as num?
                : 0;
            checkDeepEq(
                safeNumberOr(input, fallback), decodeSpecials(expected), id);
          case 'parse_boolish':
            checkDeepEq(parseBoolish(input), expected, id);
          case 'safe_food_items':
            checkDeepEq(safeFoodItems(input), decodeSpecials(expected), id);
          case 'parse_ai_json':
            if (expectsRaise(expected)) {
              expect(() => parseAiJson(input as String?),
                  throwsFormatException,
                  reason: '[$id] expected a FormatException');
            } else {
              checkDeepEq(
                  parseAiJson(input as String?), decodeSpecials(expected), id);
            }
          default:
            fail('[$id] unknown fn ${c['fn']}');
        }
      });
    }
  });

  group('nl_normalize.json', () {
    final doc = loadVectors('nl_normalize');
    test('file max_actions matches SharedConstants.nlMaxActions', () {
      expect(doc['max_actions'], SharedConstants.nlMaxActions);
    });
    for (final raw in (doc['cases'] as List).cast<Map>()) {
      final c = raw.cast<String, dynamic>();
      final id = '${c['id']}';
      test(id, () {
        expect(c['fn'], '_normalize_nl_actions');
        final actual = normalizeActions(decodeSpecials(c['input']));
        checkDeepEq(actual, decodeSpecials(c['expected']), id);
      });
    }
  });

  group('captured_at.json', () {
    final doc = loadVectors('captured_at');
    final maxAgeDays = doc['max_age_days'] as int;
    test('file max_age_days matches SharedConstants', () {
      expect(maxAgeDays, SharedConstants.capturedAtMaxAgeDaysDefault);
    });
    for (final raw in (doc['cases'] as List).cast<Map>()) {
      final c = raw.cast<String, dynamic>();
      final id = '${c['id']}';
      test(id, () {
        switch (c['fn']) {
          case 'captured_at_from_filename':
            final actual = capturedAtFromFilename(c['input'] as String?);
            final expected = c['expected'];
            if (expected == null) {
              expect(actual, isNull, reason: '[$id] expected null, got $actual');
            } else {
              expect(actual, isNotNull, reason: '[$id] expected $expected');
              expect(fmtDateTime(actual!), expected, reason: '[$id]');
            }
          case 'captured_at_valid':
            // The vectors exercise the full Python _parse_captured_at:
            // strict string parse AND the validation window.
            final now = DateTime.parse('${c['now']}');
            final valid = parseCapturedAt('${c['captured_at']}',
                    now: now, maxAgeDays: maxAgeDays) !=
                null;
            expect(valid, c['expected_valid'], reason: '[$id]');
          default:
            fail('[$id] unknown fn ${c['fn']}');
        }
      });
    }
  });

  group('mismatch.json', () {
    final doc = loadVectors('mismatch');
    for (final raw in (doc['cases'] as List).cast<Map>()) {
      final c = raw.cast<String, dynamic>();
      final id = '${c['id']}';
      test(id, () {
        expect(c['fn'], 'meal_calorie_mismatch');
        final actual = mealCalorieMismatch(decodeSpecials(c['input']));
        checkDeepEq(actual, c['expected'], id);
      });
    }
  });

  group('report_formulas.json', () {
    final doc = loadVectors('report_formulas');
    List<Map<String, dynamic>> rows(dynamic input) =>
        (decodeSpecials(input) as List)
            .map((e) => (e as Map).cast<String, dynamic>())
            .toList();
    for (final raw in (doc['cases'] as List).cast<Map>()) {
      final c = raw.cast<String, dynamic>();
      final id = '${c['id']}';
      test(id, () {
        final expected = decodeSpecials(c['expected']);
        switch (c['fn']) {
          case 'day_calorie_totals':
            checkDeepEq(dayCalorieTotals(rows(c['input'])), expected, id);
          case 'typical_day_median':
            final totals = (decodeSpecials(c['input']) as Map)
                .map((k, v) => MapEntry('$k', v as num));
            checkDeepEq(typicalDayMedian(totals), expected, id);
          case 'report_prior_avg':
            final r = reportPriorAvg(rows(c['input']));
            checkDeepEq(
                {'day_totals': r.dayTotals, 'avg': r.avg}, expected, id);
          default:
            fail('[$id] unknown fn ${c['fn']}');
        }
      });
    }
  });
}
