// Boilerplate driver so integration tests can run in --release mode via
// `flutter drive` (flutter test builds DEBUG only — the R8-minified release
// build is the variant that actually ships, and the WorkManager crash proved
// they can differ).
import 'package:integration_test/integration_test_driver.dart';

Future<void> main() => integrationDriver();
