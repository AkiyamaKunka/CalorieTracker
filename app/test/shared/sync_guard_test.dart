/// Drift gate: the generated shared bindings (app/lib/core/shared_generated
/// .dart and shared_generated.py) must match shared/ exactly. Runs
/// `python3 scripts/sync_shared.py --check`; skips with a warning when
/// python3 is unavailable (e.g. a bare CI image) rather than failing.
library;

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

Directory _repoRoot() {
  var dir = Directory.current.absolute;
  while (true) {
    if (File('${dir.path}/scripts/sync_shared.py').existsSync()) return dir;
    final parent = dir.parent;
    if (parent.path == dir.path) {
      throw StateError(
          'scripts/sync_shared.py not found above ${Directory.current.path}');
    }
    dir = parent;
  }
}

void main() {
  test('shared_generated bindings are in sync with shared/', () async {
    final root = _repoRoot();
    final ProcessResult result;
    try {
      result = await Process.run(
        'python3',
        ['${root.path}/scripts/sync_shared.py', '--check'],
        workingDirectory: root.path,
      );
    } on ProcessException {
      // ignore: avoid_print
      print('WARNING: python3 not found — skipping shared sync guard.');
      return;
    }
    expect(result.exitCode, 0,
        reason: 'scripts/sync_shared.py --check failed — a generated shared '
            'binding is out of date. Run: python3 scripts/sync_shared.py\n'
            'stdout: ${result.stdout}\nstderr: ${result.stderr}');
  });
}
