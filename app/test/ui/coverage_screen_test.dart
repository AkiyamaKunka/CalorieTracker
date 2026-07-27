// Coverage screen flow: run check → summary; "Log all" pushes each missing
// photo through the injected pipeline callback and re-audits.
import 'package:calorie_tracker/core/contracts.dart';
import 'package:calorie_tracker/services/photo/coverage.dart';
import 'package:calorie_tracker/ui/photo_pipeline.dart';
import 'package:calorie_tracker/ui/screens/coverage_screen.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import '../photo/watcher_test.dart' show FakeAsset, FakePhotoLibrary, bytesOf;
import 'fakes.dart';

Future<String> testHash(List<int> bytes) async => 'h${bytes.first}';

void main() {
  late FakePhotoLibrary library;
  late FakeDao dao;
  late CoverageAuditor auditor;
  final processed = <IntakePhoto>[];

  setUp(() {
    library = FakePhotoLibrary();
    dao = FakeDao();
    processed.clear();
    auditor = CoverageAuditor(
        library: library,
        dao: dao,
        hasher: testHash,
        clock: () => DateTime(2026, 7, 26, 12, 0));
  });

  Widget host() => MaterialApp(
        home: CoverageScreen(
          auditor: auditor,
          processPhoto: (photo) async {
            processed.add(photo);
            // Simulate the pipeline logging it: ledger gains the hash.
            dao.ledger[await testHash(photo.bytes)] = IngestionStatus.saved;
            return const PhotoOutcome(PhotoOutcomeKind.saved, 'ok');
          },
          requestPhotoPermission: () async => true,
          initialLookbackDays: 2,
          library: library,
        ),
      );

  testWidgets('run check → summary; log all → processed and re-audited',
      (tester) async {
    library.assets = [
      FakeAsset('a1', 'IMG_1.jpg', DateTime(2026, 7, 26, 8), bytesOf(1)),
      FakeAsset('a2', 'IMG_2.jpg', DateTime(2026, 7, 26, 9), bytesOf(2)),
    ];
    dao.ledger['h1'] = IngestionStatus.saved;

    await tester.pumpWidget(host());
    await tester.tap(find.byKey(const Key('runCoverageCheck')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('coverageSummary')), findsOneWidget);
    expect(find.textContaining('1 never scanned'), findsOneWidget);
    expect(find.text('IMG_2.jpg'), findsOneWidget);

    await tester.tap(find.byKey(const Key('logAllMissing')));
    await tester.pumpAndSettle();

    expect(processed, hasLength(1));
    expect(processed.single.assetId, 'a2');
    expect(processed.single.deliberate, isTrue);
    // Re-audit after acting: now fully covered.
    expect(find.textContaining('accounted for'), findsOneWidget);
    expect(find.byKey(const Key('logAllMissing')), findsNothing);
  });

  testWidgets('denied permission shows the error and never scans',
      (tester) async {
    library.assets = [
      FakeAsset('a1', 'IMG_1.jpg', DateTime(2026, 7, 26, 8), bytesOf(1)),
    ];
    await tester.pumpWidget(MaterialApp(
      home: CoverageScreen(
        auditor: auditor,
        processPhoto: (p) async =>
            const PhotoOutcome(PhotoOutcomeKind.saved, 'ok'),
        requestPhotoPermission: () async => false,
        initialLookbackDays: 2,
      ),
    ));
    await tester.tap(find.byKey(const Key('runCoverageCheck')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('coverageError')), findsOneWidget);
    expect(find.byKey(const Key('coverageSummary')), findsNothing);
  });

  testWidgets('fully covered day shows the green summary with no actions',
      (tester) async {
    library.assets = [
      FakeAsset('a1', 'IMG_1.jpg', DateTime(2026, 7, 26, 8), bytesOf(1)),
    ];
    dao.ledger['h1'] = IngestionStatus.skipped;
    await tester.pumpWidget(host());
    await tester.tap(find.byKey(const Key('runCoverageCheck')));
    await tester.pumpAndSettle();
    expect(find.textContaining('accounted for'), findsOneWidget);
    expect(find.byKey(const Key('logAllMissing')), findsNothing);
    expect(find.byKey(const Key('retryAllFailed')), findsNothing);
  });

  testWidgets('skipped photos can be RE-ANALYZED (improved rules revisit them)',
      (tester) async {
    // A 'skipped' tombstone is permanent for the automated path, so after the
    // prompt learned to accept takeout order screenshots (2026-07-27) the
    // only way to revisit them is a deliberate re-add.
    library.assets = [
      FakeAsset('a9', 'Screenshot_order.jpg', DateTime(2026, 7, 26, 8),
          bytesOf(9)),
    ];
    dao.ledger['h9'] = IngestionStatus.skipped;
    await tester.pumpWidget(host());
    await tester.tap(find.byKey(const Key('runCoverageCheck')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('reanalyzeAllSkipped')), findsOneWidget);

    await tester.tap(find.byKey(const Key('reanalyzeAllSkipped')));
    await tester.pumpAndSettle();
    expect(processed.single.assetId, 'a9');
    expect(processed.single.deliberate, isTrue,
        reason: 'only a deliberate re-add reclaims a skipped ledger row');
  });

  testWidgets('failed photos get a Retry all that goes through the pipeline',
      (tester) async {
    library.assets = [
      FakeAsset('a3', 'IMG_3.jpg', DateTime(2026, 7, 26, 8), bytesOf(3)),
    ];
    dao.ledger['h3'] = IngestionStatus.failed;
    await tester.pumpWidget(host());
    await tester.tap(find.byKey(const Key('runCoverageCheck')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('retryAllFailed')), findsOneWidget);

    await tester.tap(find.byKey(const Key('retryAllFailed')));
    await tester.pumpAndSettle();
    expect(processed.single.assetId, 'a3');
    expect(find.textContaining('accounted for'), findsOneWidget);
  });
}
