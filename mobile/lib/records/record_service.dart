import '../services/backend_service.dart';
import 'record_models.dart';

abstract class RecordService {
  bool get isPreview;
  RecordPolicy get policy;
  Future<List<RecordEntry>> list();
  // Implementations reconcile ambiguous responses using upload.id before retry.
  // A successful step advances the stage; completed steps are never resent.
  Future<void> upload(RecordUpload upload, void Function() onStageChanged);
  bool get canRetryProcessing => false;
  Future<void> retryProcessing(String id) =>
      throw UnsupportedError('Retry API not agreed');
}

/// Session-owned, metadata-only preview. Never sends or retains file contents.
class PreviewRecordService extends RecordService {
  final _entries = <String, RecordEntry>{};
  @override
  bool get isPreview => true;
  @override
  RecordPolicy get policy => const RecordPolicy(
    maxBytes: 25 * 1024 * 1024,
    consentVersion: 'preview-personalization-v1',
  );
  @override
  Future<List<RecordEntry>> list() async => _entries.values.toList();
  @override
  Future<void> upload(
    RecordUpload upload,
    void Function() onStageChanged,
  ) async {
    final validation = policy.validate(upload.file);
    if (validation != null) throw StateError(validation);
    for (final stage in [
      UploadStage.transfer,
      UploadStage.complete,
      UploadStage.done,
    ]) {
      if (upload.stage.index >= stage.index) continue;
      await Future<void>.delayed(const Duration(milliseconds: 350));
      upload.stage = stage;
      onStageChanged();
    }
    _entries.putIfAbsent(
      upload.id,
      () => RecordEntry(
        id: upload.id,
        name: upload.file.name,
        kind: upload.file.kind,
        status: RecordStatus.ready,
        createdAt: upload.consentAt,
        sizeBytes: upload.file.sizeBytes,
      ),
    );
  }
}

class SessionRecordService extends RecordService {
  SessionRecordService({required this.backend, required this.delegate});
  final BackendService backend;
  final RecordService delegate;
  @override
  bool get isPreview => delegate.isPreview;
  @override
  RecordPolicy get policy => delegate.policy;
  @override
  bool get canRetryProcessing => delegate.canRetryProcessing;
  @override
  Future<List<RecordEntry>> list() => backend.run(delegate.list);
  @override
  Future<void> upload(RecordUpload upload, void Function() onStageChanged) =>
      backend.run(() => delegate.upload(upload, onStageChanged));
  @override
  Future<void> retryProcessing(String id) =>
      backend.run(() => delegate.retryProcessing(id));
}
