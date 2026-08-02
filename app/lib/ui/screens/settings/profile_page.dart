/// Dietary profile subpage (spec §1.3): the free-text preferences appended
/// to every photo-analysis prompt. One field, one footer — its own page so
/// the Settings root stays a list of STATE, not forms.
library;

import 'package:flutter/material.dart';

import '../../services.dart' show SettingsStore;
import '../../widgets/grouped.dart';

class ProfileSettingsPage extends StatefulWidget {
  const ProfileSettingsPage({super.key, required this.settings});
  final SettingsStore settings;

  @override
  State<ProfileSettingsPage> createState() => _ProfileSettingsPageState();
}

class _ProfileSettingsPageState extends State<ProfileSettingsPage> {
  late final TextEditingController _controller;

  @override
  void initState() {
    super.initState();
    _controller =
        TextEditingController(text: widget.settings.dietaryProfile);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return GroupedPage(
      title: 'Dietary Profile',
      children: [
        GroupedSection(
          footer: 'Preferences and cultural context the AI reads alongside '
              'every photo — e.g. "vegetarian", "Cantonese home cooking, '
              'light oil", "cutting, high protein". Saves as you type.',
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
              child: TextField(
                key: const Key('dietaryProfileField'),
                controller: _controller,
                maxLines: 5,
                onChanged: (v) =>
                    widget.settings.update(dietaryProfile: v),
                decoration: const InputDecoration(
                  hintText: 'Nothing yet — the AI assumes no special '
                      'preferences.',
                  border: InputBorder.none,
                ),
              ),
            ),
          ],
        ),
      ],
    );
  }
}
