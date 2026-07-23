import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// Release-manifest drift gate.
///
/// Flutter auto-injects INTERNET into the DEBUG and PROFILE manifests (for
/// hot reload / VM service), so debug- and profile-mode testing can never
/// catch a missing network permission — the release APK shipped without
/// internet once and every Gemini call died with "Error contacting Gemini"
/// while all E2E stayed green. The MAIN manifest must declare what release
/// actually needs.
void main() {
  test('main AndroidManifest declares the permissions release needs', () {
    var dir = Directory.current;
    while (!File('${dir.path}/pubspec.yaml').existsSync()) {
      dir = dir.parent;
    }
    final manifest =
        File('${dir.path}/android/app/src/main/AndroidManifest.xml')
            .readAsStringSync();
    for (final permission in [
      'android.permission.INTERNET',
      'android.permission.READ_MEDIA_IMAGES',
      'android.permission.POST_NOTIFICATIONS',
    ]) {
      expect(manifest.contains(permission), isTrue,
          reason: '$permission missing from the MAIN manifest — debug/profile '
              'builds get it auto-injected, release does not.');
    }
  });
}
