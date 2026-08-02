/// Apple-style grouped inset lists — the Settings restructure's foundation.
///
/// Anatomy per Apple's HIG and the iOS Settings app (2026 pass): sections
/// are inset rounded cards on a *grouped* background, headers are quiet
/// uppercase captions, and FOOTERS carry the helper sentence that Material
/// screens scatter as inline paragraphs. Rows are ≥44pt with a 29pt
/// icon badge, label, trailing value, and a chevron when they disclose.
/// Forms live one level down (progressive disclosure): the root shows
/// STATE, subpages hold controls.
///
/// Hand-rolled rather than CupertinoListSection so both platforms render
/// identically inside MaterialApp and inherit our clay scheme.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show HapticFeedback;

/// Apple's ACTUAL neutral system values, not seed-derived: deriving these
/// from the clay scheme tinted every page pink (user verdict 2026-08-02).
/// Clay stays the ACCENT; surfaces are neutral — which is also exactly
/// how Apple does it (systemGroupedBackground #F2F2F7 / #000000).
Color groupedBackground(ColorScheme scheme) =>
    scheme.brightness == Brightness.dark
        ? const Color(0xFF000000)
        : const Color(0xFFF2F2F7);

/// secondarySystemGroupedBackground: the cells (#FFFFFF / #1C1C1E).
Color cellBackground(ColorScheme scheme) =>
    scheme.brightness == Brightness.dark
        ? const Color(0xFF1C1C1E)
        : const Color(0xFFFFFFFF);

/// One inset grouped section: optional header, rounded cell stack with
/// hairline separators, optional footer.
/// Shared cell geometry — ONE gutter, ONE radius everywhere.
const double kGroupInset = 16;
const double kCellRadius = 18;

/// A standalone grouped cell (hero cards, meal cards): same geometry and
/// background as a GroupedSection's cell stack, with an optional tap whose
/// ripple always matches the cell radius.
class GroupedCard extends StatelessWidget {
  const GroupedCard({
    super.key,
    required this.child,
    this.onTap,
    this.margin = const EdgeInsets.fromLTRB(kGroupInset, 12, kGroupInset, 0),
  });
  final Widget child;
  final VoidCallback? onTap;
  final EdgeInsetsGeometry margin;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: margin,
      child: Material(
        color: cellBackground(scheme),
        borderRadius: BorderRadius.circular(kCellRadius),
        clipBehavior: Clip.antiAlias,
        child: onTap == null
            ? child
            : InkWell(
                onTap: () {
                  HapticFeedback.selectionClick();
                  onTap!();
                },
                child: child,
              ),
      ),
    );
  }
}

class GroupedSection extends StatelessWidget {
  const GroupedSection({
    super.key,
    this.header,
    this.footer,
    this.separatorInset = 57,
    required this.children,
  });

  final String? header;

  /// 57 aligns past an icon badge; rows WITHOUT badges pass 16 so the
  /// hairline aligns with their text (Apple's rule: label-aligned).
  final double separatorInset;

  /// Apple puts explanations UNDER the group, small and quiet — not inside
  /// rows and not as paragraphs between fields.
  final String? footer;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 20, 16, 0),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (header != null)
            Padding(
              padding: const EdgeInsets.only(left: 16, bottom: 7),
              // iOS 26 dropped the all-caps footnote headers: sentence
              // case, a size up, slightly bolder.
              child: Text(
                header!,
                style: theme.textTheme.titleSmall?.copyWith(
                  color: scheme.onSurfaceVariant,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          Material(
            color: cellBackground(scheme),
            // iOS 26 'concentric' era: cells went from 10pt to ~26pt;
            // 18 reads current without going balloon.
            borderRadius: BorderRadius.circular(18),
            clipBehavior: Clip.antiAlias,
            child: Column(
              children: [
                for (var i = 0; i < children.length; i++) ...[
                  if (i > 0)
                    Padding(
                      // Separator inset aligns with the label, past the
                      // icon badge (Apple's 60pt-with-icon rule
                      // approximated for our 16+29+12 leading).
                      padding: EdgeInsets.only(left: separatorInset),
                      child: Divider(
                          height: 1,
                          thickness: 0.33, // Apple hairline (1px @3x)
                          color: scheme.outlineVariant
                              .withValues(alpha: 0.6)),
                    ),
                  children[i],
                ],
              ],
            ),
          ),
          if (footer != null)
            Padding(
              padding: const EdgeInsets.only(left: 16, top: 7),
              child: Text(
                footer!,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: scheme.onSurfaceVariant,
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// The 29pt rounded-square icon badge every iOS Settings row leads with.
class RowBadge extends StatelessWidget {
  const RowBadge({super.key, required this.icon, required this.color});
  final IconData icon;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 29,
      height: 29,
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.circular(7),
      ),
      child: Icon(icon, size: 18, color: Colors.white),
    );
  }
}

/// One grouped row: badge · title · value · chevron/trailing. ≥44pt,
/// selection haptic on tap (Apple's interactions are FELT, not only seen).
class GroupedRow extends StatelessWidget {
  const GroupedRow({
    super.key,
    this.icon,
    this.iconColor,
    required this.title,
    this.value,
    this.trailing,
    this.onTap,
    this.showChevron,
    this.destructive = false,
    this.enabled = true,
  });

  final IconData? icon;
  final Color? iconColor;
  final String title;

  /// The current state, shown where iOS shows it: right side, secondary.
  final String? value;
  final Widget? trailing;
  final VoidCallback? onTap;

  /// Defaults to true when [onTap] is set and no custom [trailing].
  final bool? showChevron;
  final bool destructive;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final chevron = showChevron ?? (onTap != null && trailing == null);
    final titleColor = destructive
        ? scheme.error
        : enabled
            ? scheme.onSurface
            : scheme.onSurface.withValues(alpha: 0.38);
    return InkWell(
      onTap: !enabled || onTap == null
          ? null
          : () {
              HapticFeedback.selectionClick();
              onTap!();
            },
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 44),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
          child: Row(
            children: [
              if (icon != null) ...[
                RowBadge(icon: icon!, color: iconColor ?? scheme.primary),
                const SizedBox(width: 12),
              ],
              Expanded(
                child: Text(
                  title,
                  style: theme.textTheme.bodyLarge
                      ?.copyWith(color: titleColor),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              if (value != null) ...[
                const SizedBox(width: 12),
                // NOT Flexible: a flex value would split the row 50/50
                // with the title and truncate "Backfill Lookback" beside
                // a five-character value. The cap keeps a runaway value
                // from crushing the title instead.
                ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 170),
                  child: Text(
                    value!,
                    style: theme.textTheme.bodyLarge
                        ?.copyWith(color: scheme.onSurfaceVariant),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    textAlign: TextAlign.end,
                  ),
                ),
              ],
              if (trailing != null) ...[
                const SizedBox(width: 8),
                trailing!,
              ],
              if (chevron) ...[
                const SizedBox(width: 6),
                Icon(Icons.chevron_right,
                    size: 20,
                    color: scheme.onSurfaceVariant.withValues(alpha: 0.6)),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

/// A grouped toggle row: iOS-style switch, haptic on change.
class GroupedToggleRow extends StatelessWidget {
  const GroupedToggleRow({
    super.key,
    this.icon,
    this.iconColor,
    required this.title,
    required this.value,
    required this.onChanged,
    this.switchKey,
  });

  final IconData? icon;
  final Color? iconColor;
  final String title;
  final bool value;
  final ValueChanged<bool> onChanged;

  /// Key on the SWITCH itself — existing tests find toggles by key.
  final Key? switchKey;

  @override
  Widget build(BuildContext context) {
    return GroupedRow(
      icon: icon,
      iconColor: iconColor,
      title: title,
      showChevron: false,
      trailing: Switch.adaptive(
        key: switchKey,
        value: value,
        onChanged: (v) {
          HapticFeedback.selectionClick();
          onChanged(v);
        },
      ),
      onTap: () => onChanged(!value),
    );
  }
}

/// Scaffold for a grouped subpage (the disclosure targets): grouped
/// background + iOS-style large-title collapsing header.
class GroupedPage extends StatelessWidget {
  const GroupedPage({super.key, required this.title, required this.children});
  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Scaffold(
      backgroundColor: groupedBackground(scheme),
      body: CustomScrollView(
        physics: const BouncingScrollPhysics(
            parent: AlwaysScrollableScrollPhysics()),
        slivers: [
          SliverAppBar.large(
            title: Text(title),
            backgroundColor: groupedBackground(scheme),
          ),
          SliverToBoxAdapter(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [...children, const SizedBox(height: 32)],
            ),
          ),
        ],
      ),
    );
  }
}
