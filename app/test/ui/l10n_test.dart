/// The Chinese localization CONTRACT (user request 2026-08-02): the
/// language setting actually re-locales the running app — at boot and
/// LIVE from the Settings picker, no restart. Every other suite pumps
/// bare MaterialApps and rides the English fallback; only this one proves
/// the real delegate + locale wiring in app.dart.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:calorie_tracker/ui/app.dart';

import 'fakes.dart';

void main() {
  testWidgets('appLanguage=zh boots the whole shell in Chinese',
      (tester) async {
    final settings = FakeSettings()..appLanguage = 'zh';
    await tester.pumpWidget(
        CalorieTrackerApp(services: makeServices(settings: settings)));
    await tester.pumpAndSettle();
    expect(find.text('今天'), findsWidgets); // tab + large title
    expect(find.text('历史'), findsOneWidget);
    expect(find.text('身体'), findsOneWidget);
    expect(find.text('设置'), findsOneWidget);
    expect(find.text('Today'), findsNothing);
  });

  testWidgets('picking 中文 in Settings re-locales the app in place',
      (tester) async {
    tester.view.physicalSize = const Size(800, 2400);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final settings = FakeSettings(); // 'system' → en under the test locale
    await tester.pumpWidget(
        CalorieTrackerApp(services: makeServices(settings: settings)));
    await tester.pumpAndSettle();
    expect(find.text('Today'), findsWidgets);

    await tester.tap(find.text('Settings'));
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.byKey(const Key('languageRow')));
    await tester.tap(find.byKey(const Key('languageRow')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('language-zh')));
    await tester.pumpAndSettle();

    expect(settings.appLanguage, 'zh');
    expect(find.text('今天'), findsOneWidget); // tab label, live
    expect(find.text('Today'), findsNothing);

    // And back — the switch is symmetric, not a one-way door.
    await tester.ensureVisible(find.byKey(const Key('languageRow')));
    await tester.tap(find.byKey(const Key('languageRow')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('language-en')));
    await tester.pumpAndSettle();
    expect(find.text('Today'), findsOneWidget);
    expect(find.text('今天'), findsNothing);
  });
}
