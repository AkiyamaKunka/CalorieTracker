/// App shell: Material 3 light+dark, Today / History / Settings navigation,
/// FAB → add flow. Also the startup-failure screen used by main.dart.
library;

import 'dart:async' show unawaited;
import 'dart:io' show File;

import 'package:flutter/foundation.dart' show kDebugMode;
import 'package:path_provider/path_provider.dart'
    show getApplicationDocumentsDirectory;

import 'package:flutter/material.dart';

import 'screens/add_flow.dart';
import 'screens/body_screen.dart';
import 'screens/history_screen.dart';
import 'screens/settings_screen.dart';
import 'screens/today_screen.dart';
import 'refresh_signal.dart';
import 'services.dart';

class CalorieTrackerApp extends StatelessWidget {
  final UiServices services;
  final GlobalKey<ScaffoldMessengerState>? messengerKey;
  const CalorieTrackerApp({super.key, required this.services, this.messengerKey});

  @override
  Widget build(BuildContext context) {
    const seed = Color(0xFF3E7D4F);
    // Design pass 2026-08-02 (10-app research, scratchpad/uiux_direction.md):
    // number-forward typography (heavy tabular numerals — the data IS the
    // interface), quiet tonal cards with a 16 radius, zero elevation. The
    // restraint is deliberate: the researched redesign backlashes (MFP 3.2→1.5
    // stars, Yazio "app for children") all came from decoration and added
    // friction, never from calm.
    ThemeData themed(Brightness brightness) {
      final scheme =
          ColorScheme.fromSeed(seedColor: seed, brightness: brightness);
      final base = ThemeData(colorScheme: scheme);
      final text = base.textTheme;
      TextStyle? nums(TextStyle? t, {FontWeight w = FontWeight.w700}) =>
          t?.copyWith(
              fontWeight: w,
              fontFeatures: const [FontFeature.tabularFigures()]);
      return base.copyWith(
        textTheme: text.copyWith(
          displaySmall: nums(text.displaySmall),
          headlineMedium: nums(text.headlineMedium),
          headlineSmall: nums(text.headlineSmall),
          titleLarge: text.titleLarge?.copyWith(fontWeight: FontWeight.w600),
        ),
        cardTheme: base.cardTheme.copyWith(
          elevation: 0,
          color: scheme.surfaceContainerLow,
          shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(16)),
        ),
        floatingActionButtonTheme: FloatingActionButtonThemeData(
          elevation: 1,
          shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(18)),
        ),
        navigationBarTheme: NavigationBarThemeData(
          // Default height (80): forcing 72 squeezed labels at large font
          // scales (review) for a saving nobody asked for.
          indicatorColor: scheme.secondaryContainer,
        ),
      );
    }

    return MaterialApp(
      title: 'CalorieTracker',
      scaffoldMessengerKey: messengerKey,
      theme: themed(Brightness.light),
      darkTheme: themed(Brightness.dark),
      themeMode: ThemeMode.system,
      home: HomeShell(services: services),
    );
  }
}

class HomeShell extends StatefulWidget {
  final UiServices services;
  const HomeShell({super.key, required this.services});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> with WidgetsBindingObserver {
  late int _index;
  final GlobalKey<TodayScreenState> _todayKey = GlobalKey<TodayScreenState>();
  // History sits in the IndexedStack, so its initState runs once at app
  // launch; reload it on tab selection or it shows launch-time data forever
  // (bug found by the e2e flow: meals logged after launch never appeared).
  final GlobalKey<HistoryScreenState> _historyKey =
      GlobalKey<HistoryScreenState>();
  final GlobalKey<BodyScreenState> _bodyKey = GlobalKey<BodyScreenState>();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    mealsChangedSignal.addListener(_onMealsChanged);
    // Onboarding: with no API key yet, land on Settings first.
    _index = widget.services.settings.apiKey.trim().isEmpty ? 3 : 0;
    // Debug-only: headless simulator screenshot runs force a tab by
    // dropping a one-byte file into the app's Documents dir (simctl has no
    // tap injection; env forwarding and HOME-derived paths both proved
    // unreliable). Async by necessity — a forced tab pops in a frame after
    // first build, which screenshot runs don't care about. Inert in release
    // builds and when the file is absent.
    if (kDebugMode) {
      unawaited(getApplicationDocumentsDirectory().then((dir) {
        final f = File('${dir.path}/ct_debug_tab');
        if (!f.existsSync() || !mounted) return;
        final forced = int.tryParse(f.readAsStringSync().trim());
        if (forced != null && forced >= 0 && forced <= 3) {
          setState(() => _index = forced);
        }
      }, onError: (Object _) {}));
    }
  }

  @override
  void dispose() {
    mealsChangedSignal.removeListener(_onMealsChanged);
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  /// A meal was saved somewhere in the app (watcher, catch-up, coverage
  /// screen). Re-query the data tabs so what's on screen matches the DB.
  void _onMealsChanged() {
    if (!mounted) return;
    _todayKey.currentState?.reload();
    _historyKey.currentState?.reload();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) return;
    final s = widget.services;
    // The background job may have logged meals while we were away — the
    // IndexedStack keeps stale screens alive, so refresh them now.
    _todayKey.currentState?.reload();
    if (_index == 1) _historyKey.currentState?.reload();
    if (!s.settings.watcherEnabled) return;
    // One predicate for "an analysis could succeed" (key + no quota latch):
    // don't read photo bytes for nothing.
    if (!s.settings.canAnalyze) return;
    // Resume catch-up (full lookback window): the change-notify watcher is
    // frozen with the process on EMUI-class OSes, so photos taken while the
    // app was backgrounded surface here. The md5 ledger + in-session seen
    // set make repeat resumes near-free. Refresh again once the scan's
    // emissions have had a chance to process.
    unawaited(s.photoIntake
        ?.backfillScan()
        .then((_) {}, onError: (Object _) {})
        .then((_) => Future<void>.delayed(const Duration(seconds: 15)))
        .then((_) {
      if (!mounted) return;
      _todayKey.currentState?.reload();
      if (_index == 1) _historyKey.currentState?.reload();
    }));
  }

  @override
  Widget build(BuildContext context) {
    final s = widget.services;
    return Scaffold(
      appBar: AppBar(
        title: Text(const ['Today', 'History', 'Body', 'Settings'][_index]),
      ),
      body: IndexedStack(
        index: _index,
        children: [
          TodayScreen(
              key: _todayKey,
              dao: s.dao,
              executor: s.executor,
              thumbs: s.thumbs,
              garminDaily: s.garminDaily,
              // The + button lives INSIDE TodayScreen (above its chat box),
              // not as a Scaffold FAB — a Scaffold FAB floats exactly over
              // the chat box's send button.
              onAdd: () => openAddFlow(context, s,
                  onChanged: () async => _todayKey.currentState?.reload())),
          HistoryScreen(key: _historyKey, dao: s.dao, thumbs: s.thumbs),
          BodyScreen(key: _bodyKey, dao: s.dao),
          SettingsScreen(
            settings: s.settings,
            analyzer: s.analyzer,
            dao: s.dao,
            requestPhotoPermission: s.requestPhotoPermission,
            photoIntake: s.photoIntake,
            coverage: s.coverage,
            processPhoto: s.processPhoto,
            photoLibrary: s.photoLibrary,
            startClaudeAuth: s.startClaudeAuth,
            completeClaudeAuth: s.completeClaudeAuth,
            openSystemSettings: s.openSystemSettings,
          ),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) {
          setState(() => _index = i);
          // BOTH data tabs reload on selection. They live in an IndexedStack,
          // so their state survives tab switches — and a meal logged while
          // the app was open (watcher, background job, or the launch/resume
          // catch-up) is invisible on a screen built before it existed. That
          // is exactly how a 07:40 latte, correctly analyzed and saved at
          // 18:06, stayed missing from Today until a manual pull-to-refresh
          // (real report, 2026-07-26).
          if (i == 0) _todayKey.currentState?.reload();
          if (i == 1) _historyKey.currentState?.reload();
          if (i == 2) _bodyKey.currentState?.reload();
        },
        destinations: const [
          NavigationDestination(
              icon: Icon(Icons.today_outlined),
              selectedIcon: Icon(Icons.today),
              label: 'Today'),
          NavigationDestination(
              icon: Icon(Icons.history), label: 'History'),
          NavigationDestination(
              icon: Icon(Icons.monitor_weight_outlined),
              selectedIcon: Icon(Icons.monitor_weight),
              label: 'Body'),
          NavigationDestination(
              icon: Icon(Icons.settings_outlined),
              selectedIcon: Icon(Icons.settings),
              label: 'Settings'),
        ],
      ),
    );
  }
}

/// Shown when async startup (settings → dao → services) throws.
class StartupErrorApp extends StatelessWidget {
  final String error;
  const StartupErrorApp({super.key, required this.error});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'CalorieTracker',
      home: Scaffold(
        body: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.error_outline, size: 48),
                const SizedBox(height: 12),
                const Text('CalorieTracker could not start.'),
                const SizedBox(height: 8),
                Text(error, textAlign: TextAlign.center),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
