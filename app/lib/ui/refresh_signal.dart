/// "The meal list changed" broadcast.
///
/// The pipeline can save a meal at ANY time — the foreground watcher, the
/// launch/resume catch-up, the coverage screen's Log/Retry, or the
/// WorkManager isolate handing over on resume. The screens live in an
/// IndexedStack and are built once, so without a signal they keep showing
/// pre-save data until something else happens to reload them: a real
/// 07:40 latte was analyzed and saved correctly at 18:06 and still looked
/// missing to the user (2026-07-26 report).
///
/// A plain counter rather than a stream: listeners only need "something
/// changed, re-query", and a ValueNotifier needs no lifecycle management.
library;

import 'package:flutter/foundation.dart';

final ValueNotifier<int> mealsChangedSignal = ValueNotifier<int>(0);

void signalMealsChanged() => mealsChangedSignal.value++;
