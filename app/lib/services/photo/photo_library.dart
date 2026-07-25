/// photo_manager seam for the photo-intake module (spec §6.1).
///
/// Everything the watcher / picker needs from the device photo library sits
/// behind [PhotoLibrary] so tests can fake it — `flutter test` has no
/// platform channels. [PhotoManagerLibrary] is the only production
/// implementation.
library;

import 'dart:typed_data';

import 'package:flutter/services.dart' show MethodCall;
import 'package:photo_manager/photo_manager.dart';

/// Spec §8: refuse photos larger than 25 MB before any decode.
const int maxPhotoBytes = 25 * 1024 * 1024;

/// One image asset. [originBytes] MUST return the ORIGINAL library bytes —
/// the md5 dedup identity is md5(original bytes), never a recompression
/// (spec §6.2).
abstract class LibraryAsset {
  String get id;
  DateTime get createDateTime;
  Future<String> fileName();
  Future<Uint8List?> originBytes();
}

abstract class PhotoLibrary {
  /// Request read access; limited/partial grants count as access.
  Future<bool> requestPermission();

  /// Small display thumbnail for a known asset id; null when the asset is
  /// gone, unreadable, or permission is missing. Used to lazily backfill
  /// meal thumbs for meals logged before thumbs existed, and for the
  /// coverage audit's missing-photo list.
  Future<Uint8List?> thumbnailByAssetId(String assetId, {int size = 160});

  /// Images whose createDate is at/after [cutoff] (spec §6.4 backfill
  /// window). Order unspecified — callers sort for emission.
  Future<List<LibraryAsset>> imagesCreatedAfter(DateTime cutoff,
      {int limit = 500});

  /// Most-recent images, newest first (in-app picker grid source).
  Future<List<LibraryAsset>> recentImages(int limit);

  /// Change-notify subscription while the app is alive (spec §6.1 watcher).
  Future<void> startChangeNotify(void Function() onChange);
  Future<void> stopChangeNotify();
}

class _PhotoManagerAsset implements LibraryAsset {
  _PhotoManagerAsset(this._entity);
  final AssetEntity _entity;

  @override
  String get id => _entity.id;

  @override
  DateTime get createDateTime => _entity.createDateTime;

  @override
  Future<String> fileName() => _entity.titleAsync;

  @override
  Future<Uint8List?> originBytes() => _entity.originBytes;
}

class PhotoManagerLibrary implements PhotoLibrary {
  void Function(MethodCall)? _changeCallback;

  @override
  Future<Uint8List?> thumbnailByAssetId(String assetId, {int size = 160}) async {
    if (assetId.isEmpty) return null;
    try {
      final entity = await AssetEntity.fromId(assetId);
      if (entity == null) return null;
      return await entity.thumbnailDataWithSize(ThumbnailSize.square(size));
    } catch (_) {
      return null; // deleted asset / revoked permission: display-degrade
    }
  }

  @override
  Future<bool> requestPermission() async {
    // Spec §6 permission flow: iOS "limited" / Android "selected photos"
    // still exposes a user-granted subset, which is enough to watch.
    final state = await PhotoManager.requestPermissionExtend();
    return state.hasAccess;
  }

  Future<List<LibraryAsset>> _query(
      FilterOptionGroup filter, int limit) async {
    final paths = await PhotoManager.getAssetPathList(
        type: RequestType.image, onlyAll: true, filterOption: filter);
    if (paths.isEmpty) return const [];
    final assets = await paths.first.getAssetListRange(start: 0, end: limit);
    return assets.map(_PhotoManagerAsset.new).toList();
  }

  @override
  Future<List<LibraryAsset>> imagesCreatedAfter(DateTime cutoff,
      {int limit = 500}) {
    final filter = FilterOptionGroup(
      createTimeCond: DateTimeCond(
        min: cutoff,
        // Upper bound mirrors the §6.3 +1h future-skew allowance.
        max: DateTime.now().add(const Duration(hours: 1)),
      ),
      orders: [const OrderOption(type: OrderOptionType.createDate, asc: false)],
    );
    return _query(filter, limit);
  }

  @override
  Future<List<LibraryAsset>> recentImages(int limit) {
    final filter = FilterOptionGroup(
      orders: [const OrderOption(type: OrderOptionType.createDate, asc: false)],
    );
    return _query(filter, limit);
  }

  @override
  Future<void> startChangeNotify(void Function() onChange) async {
    _changeCallback ??= (MethodCall _) => onChange();
    PhotoManager.addChangeCallback(_changeCallback!);
    await PhotoManager.startChangeNotify();
  }

  @override
  Future<void> stopChangeNotify() async {
    final cb = _changeCallback;
    if (cb != null) {
      PhotoManager.removeChangeCallback(cb);
      _changeCallback = null;
    }
    await PhotoManager.stopChangeNotify();
  }
}
