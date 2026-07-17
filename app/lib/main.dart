/// Entry point: async init (settings → dao → reclaimStaleProcessing →
/// services, wired in ui/di.dart), then the Material app shell.
library;

import 'package:flutter/material.dart';

import 'ui/app.dart';
import 'ui/di.dart';

final GlobalKey<ScaffoldMessengerState> messengerKey =
    GlobalKey<ScaffoldMessengerState>();

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    final services = await AppServices.init(
      // Notification-style snackbar for background photo saves (spec §6).
      notify: (msg) => messengerKey.currentState
          ?.showSnackBar(SnackBar(content: Text(msg))),
    );
    runApp(CalorieTrackerApp(services: services.ui, messengerKey: messengerKey));
  } catch (e) {
    // Startup must never leave a blank screen; surface the failure.
    runApp(StartupErrorApp(error: '$e'));
  }
}
