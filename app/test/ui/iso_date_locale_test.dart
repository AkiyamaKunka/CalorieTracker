// meals.date is WRITTEN by the photo and editor paths and then compared
// with SQL string ranges ('date >= ? AND date <= ?'), grouped by, and sent
// to the server's formatters. So it must be Gregorian with ASCII digits no
// matter what locale the phone is set to.
//
// The regression this guards: ui/format.dart used
// `DateFormat('yyyy-MM-dd')`, which renders the digits in the ambient
// locale's numbering system. MEASURED with Dart's intl for 2026-07-03:
//   en_US, th, ar → 2026-07-03   (fine)
//   fa            → ۲۰۲۶-۰۷-۰۳   (Extended Arabic-Indic)
//   ne            → २०२६-०७-०३   (Devanagari)
//   my            → ၂၀၂၆-၀၇-၀၃   (Myanmar)
// Those strings match no `date >= ? AND date <= ?` range, no grouping key
// and no server formatter — and this is the PHOTO/EDITOR WRITE path, so the
// corruption would be permanent in the user's log. Latent today only
// because the app never sets Intl.defaultLocale; adding localization (or a
// plugin that sets it) would have silently armed it.
import 'package:calorie_tracker/data/meals_logic.dart' as logic;
import 'package:calorie_tracker/ui/format.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/intl.dart';

void main() {
  final d = DateTime(2026, 7, 3, 21, 5);

  test('isoDate is plain ASCII Gregorian', () {
    expect(isoDate(d), '2026-07-03');
    expect(isoDate(DateTime(999, 1, 2)), '0999-01-02'); // zero-padded year
  });

  test('and stays that way under a hostile ambient locale', () {
    final previous = Intl.defaultLocale;
    addTearDown(() => Intl.defaultLocale = previous);
    // fa/ne/my are the locales that actually reproduced the old bug.
    for (final locale in ['fa', 'ne', 'my', 'th', 'ar', 'en_US']) {
      Intl.defaultLocale = locale;
      expect(isoDate(d), '2026-07-03', reason: 'locale $locale');
    }
  });

  test('the ui and data layers agree — one implementation, not two', () {
    expect(isoDate(d), logic.isoDate(d));
  });
}
