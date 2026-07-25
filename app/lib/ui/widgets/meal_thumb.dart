/// Leading photo square for meal rows: the stored/backfilled thumbnail, or
/// a typed placeholder (camera icon for photo meals whose picture is gone,
/// notes icon for text/manual meals). Fixed size so list rows never jump
/// when the future resolves.
library;

import 'dart:typed_data';

import 'package:flutter/material.dart';

class MealThumbView extends StatelessWidget {
  const MealThumbView({
    super.key,
    required this.thumb,
    required this.isPhotoMeal,
    this.size = 52,
  });

  /// Resolved thumbnail future (null future value → placeholder).
  final Future<Uint8List?>? thumb;
  final bool isPhotoMeal;
  final double size;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    Widget placeholder() => Container(
          width: size,
          height: size,
          decoration: BoxDecoration(
            color: scheme.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(10),
          ),
          child: Icon(
            isPhotoMeal ? Icons.photo_camera_outlined : Icons.notes_outlined,
            size: size * 0.45,
            color: scheme.onSurfaceVariant,
          ),
        );
    if (thumb == null) return placeholder();
    return FutureBuilder<Uint8List?>(
      future: thumb,
      builder: (context, snap) {
        final bytes = snap.data;
        if (bytes == null || bytes.isEmpty) return placeholder();
        return ClipRRect(
          borderRadius: BorderRadius.circular(10),
          child: Image.memory(
            bytes,
            key: const Key('mealThumbImage'),
            width: size,
            height: size,
            fit: BoxFit.cover,
            gaplessPlayback: true, // no flash on rebuild
            errorBuilder: (_, _, _) => placeholder(),
          ),
        );
      },
    );
  }
}
