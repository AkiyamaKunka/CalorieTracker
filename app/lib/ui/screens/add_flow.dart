/// Add flow (FAB): pick a recent photo (grid → analyzing spinner → meal
/// card, spec §2.3/§3/§6 pipeline with deliberate=true) or paste text
/// (NlExecutor → NlReply list incl. the delete-confirmation modal, spec §4).
library;

import 'package:flutter/material.dart';

import '../../core/contracts.dart';
import '../nl_presenter.dart';
import '../photo_pipeline.dart';
import '../services.dart';

/// Entry point wired to the FAB. [onChanged] refreshes the Today screen.
Future<void> openAddFlow(BuildContext context, UiServices services,
    {Future<void> Function()? onChanged}) async {
  final choice = await showModalBottomSheet<String>(
    context: context,
    builder: (ctx) => SafeArea(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          ListTile(
            key: const Key('addFromPhotos'),
            leading: const Icon(Icons.photo_library_outlined),
            title: const Text('From recent photos'),
            onTap: () => Navigator.of(ctx).pop('photos'),
          ),
          ListTile(
            key: const Key('addFromText'),
            leading: const Icon(Icons.edit_note),
            title: const Text('Describe by text'),
            onTap: () => Navigator.of(ctx).pop('text'),
          ),
        ],
      ),
    ),
  );
  if (!context.mounted) return;
  if (choice == 'photos') {
    await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => AddPhotoScreen(services: services)));
  } else if (choice == 'text') {
    await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => AddTextScreen(executor: services.executor)));
  } else {
    return;
  }
  await onChanged?.call();
}

/// Recent-photos grid → tap → analyzing spinner → result card.
class AddPhotoScreen extends StatefulWidget {
  final UiServices services;
  const AddPhotoScreen({super.key, required this.services});

  @override
  State<AddPhotoScreen> createState() => _AddPhotoScreenState();
}

class _AddPhotoScreenState extends State<AddPhotoScreen> {
  late Future<List<IntakePhoto>> _photos;
  bool _analyzing = false;

  @override
  void initState() {
    super.initState();
    _photos = widget.services.picker.recentPhotos();
  }

  Future<void> _analyze(IntakePhoto photo) async {
    setState(() => _analyzing = true);
    final pipeline = PhotoPipeline(
        dao: widget.services.dao, analyzer: widget.services.analyzer);
    // User-picked → deliberate: reclaims failed/skipped/deleted ledger rows
    // (spec §2.3 caller policies).
    final deliberate = IntakePhoto(photo.bytes, photo.assetId, photo.fileName,
        capturedAt: photo.capturedAt, deliberate: true);
    final outcome = await pipeline.process(deliberate);
    if (!mounted) return;
    setState(() => _analyzing = false);
    await showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(switch (outcome.kind) {
          PhotoOutcomeKind.saved => 'Meal logged',
          PhotoOutcomeKind.skipped => 'No food detected',
          PhotoOutcomeKind.duplicate => 'Duplicate photo',
          PhotoOutcomeKind.alreadyTracked => 'Already logged',
          PhotoOutcomeKind.failed => 'Analysis failed',
        }),
        content: Text(outcome.message),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('OK'),
          ),
        ],
      ),
    );
    if (outcome.kind == PhotoOutcomeKind.saved && mounted) {
      Navigator.of(context).pop();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Recent photos')),
      body: Stack(
        children: [
          FutureBuilder<List<IntakePhoto>>(
            future: _photos,
            builder: (context, snap) {
              if (snap.connectionState != ConnectionState.done) {
                return const Center(child: CircularProgressIndicator());
              }
              if (snap.hasError) {
                return Center(
                    child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Text('Could not load photos: ${snap.error}'),
                ));
              }
              final photos = snap.data ?? const [];
              if (photos.isEmpty) {
                return const Center(child: Text('No recent photos found.'));
              }
              return GridView.builder(
                padding: const EdgeInsets.all(8),
                gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                    crossAxisCount: 3, crossAxisSpacing: 4, mainAxisSpacing: 4),
                itemCount: photos.length,
                itemBuilder: (context, i) => GestureDetector(
                  key: Key('recentPhoto$i'),
                  onTap: _analyzing ? null : () => _analyze(photos[i]),
                  child: Image.memory(photos[i].bytes,
                      fit: BoxFit.cover, gaplessPlayback: true),
                ),
              );
            },
          ),
          if (_analyzing)
            Container(
              color: Colors.black45,
              child: const Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    CircularProgressIndicator(key: Key('photoAnalyzing')),
                    SizedBox(height: 12),
                    Text('Analyzing…',
                        style: TextStyle(color: Colors.white)),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// Paste-text meal entry feeding the NL executor (spec §4.6 new_meal path,
/// but any intent works — replies render through the shared presenter).
class AddTextScreen extends StatefulWidget {
  final NlExecutor executor;
  const AddTextScreen({super.key, required this.executor});

  @override
  State<AddTextScreen> createState() => _AddTextScreenState();
}

class _AddTextScreenState extends State<AddTextScreen> {
  final TextEditingController _controller = TextEditingController();
  bool _sending = false;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _send() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _sending) return;
    setState(() => _sending = true);
    try {
      final replies = await widget.executor.handleText(text);
      if (!mounted) return;
      await presentNlReplies(context, widget.executor, replies);
      if (mounted) Navigator.of(context).pop();
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Describe a meal')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            TextField(
              key: const Key('addTextField'),
              controller: _controller,
              maxLines: 4,
              autofocus: true,
              decoration: const InputDecoration(
                hintText: 'e.g. "two eggs and toast with butter"',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            FilledButton(
              key: const Key('addTextSend'),
              onPressed: _sending ? null : _send,
              child: _sending
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(strokeWidth: 2))
                  : const Text('Log it'),
            ),
          ],
        ),
      ),
    );
  }
}
