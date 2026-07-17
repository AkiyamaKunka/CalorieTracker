/// Schema DDL pinning tests (spec §2.1) — verify the ported table set and
/// load-bearing clauses without opening a platform database.
library;

import 'package:calorie_tracker/data/db.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  String ddlFor(String table) => kSchemaDdl.firstWhere(
      (s) => s.contains('CREATE TABLE IF NOT EXISTS $table'));

  test('all spec §2.1 tables are created (heartbeats deliberately omitted)',
      () {
    for (final table in [
      'meals',
      'photo_ingestions',
      'body_weight',
      'workouts',
      'activities',
      'fitness_profile',
    ]) {
      expect(() => ddlFor(table), returnsNormally, reason: table);
    }
    // Spec §2.1/§2.2: heartbeats is a server-only concept — no app role.
    expect(kSchemaDdl.any((s) => s.contains('heartbeats')), isFalse);
  });

  test('meals indexes on (chat_id,date) and (chat_id,image_hash) exist', () {
    expect(
        kSchemaDdl.any((s) =>
            s.contains('idx_chat_date') && s.contains('meals(chat_id, date)')),
        isTrue);
    expect(
        kSchemaDdl.any((s) =>
            s.contains('idx_chat_hash') &&
            s.contains('meals(chat_id, image_hash)')),
        isTrue);
  });

  test('photo_ingestions keys one row per (chat_id, image_hash)', () {
    expect(ddlFor('photo_ingestions'),
        contains('PRIMARY KEY (chat_id, image_hash)'));
    expect(ddlFor('photo_ingestions'), contains("DEFAULT 'processing'"));
  });

  test('body_weight enforces one weigh-in per user-local day', () {
    expect(ddlFor('body_weight'), contains('UNIQUE(chat_id, date)'));
  });

  test('activities upsert key is (chat_id, source, external_id)', () {
    expect(ddlFor('activities'),
        contains('UNIQUE(chat_id, source, external_id)'));
  });

  test('export covers every table in DDL order', () {
    expect(kExportTables, [
      'meals',
      'photo_ingestions',
      'body_weight',
      'workouts',
      'activities',
      'fitness_profile',
    ]);
  });
}
