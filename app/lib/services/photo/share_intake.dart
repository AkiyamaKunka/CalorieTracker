/// Deliberate intake: share-sheet stream + in-app "recent photos" picker
/// source (spec §6, §2.3 caller policy).
///
/// Everything emitted here carries `deliberate=true`: a human re-sending a
/// photo may reclaim failed/skipped/deleted ledger rows (the "deliberate
/// re-send" rule, spec §2.3).
library;

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:receive_sharing_intent/receive_sharing_intent.dart';

import '../../core/contracts.dart';
import '../settings/app_settings.dart' show AppSettings;
import 'filename_dates.dart';
import 'photo_library.dart';

/// Spec §6.1: SUPPORTED_QUEUE_EXTENSIONS ∪ server-side .tiff. Content is
/// ultimately validated by decodability downstream (the analyzer's decode),
/// not by extension — this set only filters obvious non-photos out of the
/// share sheet.
const Set<String> supportedPhotoExtensions = {
  '.jpg',
  '.jpeg',
  '.png',
  '.heic',
  '.heif',
  '.tiff',
};

bool hasSupportedPhotoExtension(String name) {
  final dot = name.lastIndexOf('.');
  if (dot < 0) return false;
  return supportedPhotoExtensions.contains(name.substring(dot).toLowerCase());
}

String fileBaseName(String path) {
  final sep = path.lastIndexOf(RegExp(r'[/\\]'));
  return sep < 0 ? path : path.substring(sep + 1);
}

/// receive_sharing_intent seam so tests can fake the platform stream.
abstract class SharedMediaSource {
  /// Batches of shared image file paths while the app is alive.
  Stream<List<String>> imagePathBatches();

  /// The cold-start share that launched the app (consumed once).
  Future<List<String>> initialImagePaths();
}

class ReceiveSharingIntentSource implements SharedMediaSource {
  List<String> _imagePaths(List<SharedMediaFile> files) => files
      .where((f) => f.type == SharedMediaType.image)
      .map((f) => f.path)
      .toList();

  @override
  Stream<List<String>> imagePathBatches() =>
      ReceiveSharingIntent.instance.getMediaStream().map(_imagePaths);

  @override
  Future<List<String>> initialImagePaths() async {
    final files = await ReceiveSharingIntent.instance.getInitialMedia();
    // Consume the launch intent so a hot restart does not re-log the share.
    ReceiveSharingIntent.instance.reset();
    return _imagePaths(files);
  }
}

typedef FileBytesReader = Future<Uint8List?> Function(String path);
typedef SharedFileCleanup = Future<void> Function(String path);

Future<Uint8List?> _readFileBytes(String path) async {
  try {
    return await File(path).readAsBytes();
  } catch (_) {
    return null; // unreadable share target: skip, never crash intake
  }
}

/// The plugin COPIES every share into the app's container (iOS app-group /
/// Android app dir) and its API contract says the consumer deletes the copy
/// after use — nothing else ever purges them, so skipping this leaks the
/// full-size original of every shared photo forever (storage + privacy).
Future<void> _deleteSharedFile(String path) async {
  try {
    await File(path).delete();
  } catch (_) {
    // Best effort: a vanished/locked file must not break intake.
  }
}

/// Integration-contract factory: the integrator wires this next to
/// `createPhotoIntake` with the settings module's [AppSettings]. (Share
/// intake currently needs no settings knob — the parameter keeps the wiring
/// uniform and future-proof.)
ShareIntake createShareIntake(AppSettings settings) => ShareIntake(
    source: ReceiveSharingIntentSource(),
    library: PhotoManagerLibrary(),
    readBytes: _readFileBytes);

class ShareIntake {
  ShareIntake(
      {required this._source,
      required this._library,
      required this._readBytes,
      SharedFileCleanup? cleanup,
      DateTime Function()? clock,
      // §8 CAPTURED_AT_MAX_AGE_DAYS: default 45, not user-editable.
      this._capturedAtMaxAgeDays = 45})
      : _cleanup = cleanup ?? _deleteSharedFile,
        _clock = clock ?? DateTime.now;

  final SharedMediaSource _source;
  final PhotoLibrary _library;
  final FileBytesReader _readBytes;
  final SharedFileCleanup _cleanup;
  final DateTime Function() _clock;
  final int _capturedAtMaxAgeDays;

  /// Cold-start share first, then live shares for the app's lifetime.
  /// (Explicit controller, not async*: an async* generator parked on the
  /// live-share stream would defer subscription cancellation forever.)
  Stream<IntakePhoto> photos() {
    late final StreamController<IntakePhoto> controller;
    StreamSubscription<List<String>>? liveSub;
    var cancelled = false;

    Future<void> emitPaths(List<String> paths) async {
      for (final path in paths) {
        final photo = await _fromPath(path);
        if (photo != null && !cancelled && !controller.isClosed) {
          controller.add(photo);
        }
      }
    }

    controller = StreamController<IntakePhoto>(
      onListen: () async {
        await emitPaths(await _source.initialImagePaths());
        if (cancelled) return;
        liveSub = _source.imagePathBatches().listen(
            (batch) => unawaited(emitPaths(batch)),
            onError: controller.addError,
            onDone: controller.close);
      },
      onCancel: () async {
        cancelled = true;
        await liveSub?.cancel();
      },
    );
    return controller.stream;
  }

  Future<IntakePhoto?> _fromPath(String path) async {
    try {
      final name = fileBaseName(path);
      if (!hasSupportedPhotoExtension(name)) return null; // §6.1
      final bytes = await _readBytes(path);
      if (bytes == null || bytes.isEmpty) return null;
      if (bytes.length > maxPhotoBytes) return null; // §8: 25 MB cap
      // A shared file has no library asset date; filename timestamp or bust
      // (§6.3 fallback = intake-time dating, expressed as capturedAt null).
      final capturedAt = deriveCapturedAt(
          fileName: name, now: _clock(), maxAgeDays: _capturedAtMaxAgeDays);
      return IntakePhoto(bytes, '', name,
          capturedAt: capturedAt, deliberate: true);
    } finally {
      // Always remove the plugin's container copy — accepted, rejected, or
      // unreadable, it has served its purpose once we've looked at it (the
      // bytes above are already in memory when accepted).
      await _cleanup(path);
    }
  }

  /// Recent library assets for the UI's own picker grid (the UI renders the
  /// grid; this module only supplies the data). deliberate=true — a picked
  /// photo is a user decision, reclaiming failed/skipped/deleted (§2.3).
  Future<List<IntakePhoto>> pickFromRecent(int limit) async {
    if (limit <= 0) return const [];
    if (!await _library.requestPermission()) return const [];
    final assets = await _library.recentImages(limit);
    final out = <IntakePhoto>[];
    for (final asset in assets.take(limit)) {
      final bytes = await asset.originBytes(); // ORIGINAL bytes (§6.2)
      if (bytes == null || bytes.isEmpty) continue;
      if (bytes.length > maxPhotoBytes) continue;
      final name = await asset.fileName();
      final capturedAt = deriveCapturedAt(
          fileName: name,
          assetCreateDate: asset.createDateTime,
          now: _clock(),
          maxAgeDays: _capturedAtMaxAgeDays);
      out.add(IntakePhoto(bytes, asset.id, name,
          capturedAt: capturedAt, deliberate: true));
    }
    return out;
  }
}
