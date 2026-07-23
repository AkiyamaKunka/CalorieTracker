/// App shell: Material 3 light+dark, Today / History / Settings navigation,
/// FAB → add flow. Also the startup-failure screen used by main.dart.
library;

import 'dart:async' show unawaited;

import 'package:flutter/material.dart';

import 'screens/add_flow.dart';
import 'screens/history_screen.dart';
import 'screens/settings_screen.dart';
import 'screens/today_screen.dart';
import 'services.dart';

class CalorieTrackerApp extends StatelessWidget {
  final UiServices services;
  final GlobalKey<ScaffoldMessengerState>? messengerKey;
  const CalorieTrackerApp({super.key, required this.services, this.messengerKey});

  @override
  Widget build(BuildContext context) {
    const seed = Color(0xFF3E7D4F);
    return MaterialApp(
      title: 'CalorieTracker',
      scaffoldMessengerKey: messengerKey,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: seed),
      ),
      darkTheme: ThemeData(
        colorScheme:
            ColorScheme.fromSeed(seedColor: seed, brightness: Brightness.dark),
      ),
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

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    // Onboarding: with no API key yet, land on Settings first.
    _index = widget.services.settings.apiKey.trim().isEmpty ? 2 : 0;
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
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
    if (s.settings.apiKey.trim().isEmpty) return; // nothing can succeed yet
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
        title: Text(const ['Today', 'History', 'Settings'][_index]),
      ),
      body: IndexedStack(
        index: _index,
        children: [
          TodayScreen(key: _todayKey, dao: s.dao, executor: s.executor),
          HistoryScreen(key: _historyKey, dao: s.dao),
          SettingsScreen(
            settings: s.settings,
            analyzer: s.analyzer,
            dao: s.dao,
            requestPhotoPermission: s.requestPhotoPermission,
            photoIntake: s.photoIntake,
          ),
        ],
      ),
      floatingActionButton: _index == 0
          ? FloatingActionButton(
              key: const Key('addMealFab'),
              onPressed: () => openAddFlow(context, s,
                  onChanged: () async => _todayKey.currentState?.reload()),
              child: const Icon(Icons.add),
            )
          : null,
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) {
          setState(() => _index = i);
          if (i == 1) _historyKey.currentState?.reload();
        },
        destinations: const [
          NavigationDestination(
              icon: Icon(Icons.today_outlined),
              selectedIcon: Icon(Icons.today),
              label: 'Today'),
          NavigationDestination(
              icon: Icon(Icons.history), label: 'History'),
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
