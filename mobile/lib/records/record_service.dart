import '../services/backend_service.dart';
import 'record_deletion.dart';
import 'record_models.dart';
import 'record_playback.dart';
import 'record_review.dart';
import 'transcript_segments.dart';

abstract class RecordService
    implements RecordReviewService, RecordDeletionService {
  @override
  bool get canDeleteAccount => false;
  @override
  bool get canRetryDeletion => false;
  @override
  Future<DeletionJob> deleteRecord(String recordId) =>
      throw UnsupportedError('Deletion not connected');
  @override
  Future<DeletionJob> deleteAccount() =>
      throw UnsupportedError('Account deletion contract not agreed');
  @override
  Future<DeletionJob> deletionJob(String jobId) =>
      throw UnsupportedError('Deletion not connected');
  @override
  Future<List<DeletionJob>> deletionJobs() async => [];
  @override
  Future<DeletionJob> retryDeletion(DeletionJob job) =>
      throw UnsupportedError('Retry contract not agreed');
  @override
  Future<RecordReview> review(String recordId) =>
      throw UnsupportedError('Review not connected');
  @override
  Future<TextVersion> saveText(TextVersion version, String content) =>
      throw UnsupportedError('Review not connected');
  @override
  Future<TextVersion> confirmText(TextVersion version) =>
      throw UnsupportedError('Review not connected');
  bool get isPreview;
  RecordPolicy get policy;
  Future<List<RecordEntry>> list();
  // Implementations reconcile ambiguous responses using upload.id before retry.
  // A successful step advances the stage; completed steps are never resent.
  // Report transfer progress with upload.reportProgress and stop with
  // UploadCancelled once upload.cancelToken is cancelled.
  Future<void> upload(RecordUpload upload, void Function() onStageChanged);

  /// Requests cleanup of an unfinished upload and returns its deletion job.
  /// Null means no deletion job was requested, not proof of completed cleanup.
  /// Never touches a record whose upload already completed.
  Future<DeletionJob?> discardUpload(RecordUpload upload) async => null;
  bool get canRetryProcessing => false;
  Future<void> retryProcessing(String id) =>
      throw UnsupportedError('Retry API not agreed');

  /// Whether the original audio of a record can be played in the app.
  bool get canPlayOriginal => false;
  Future<RecordAudioSource> originalAudio(String recordId) =>
      throw UnsupportedError('Original audio endpoint not agreed');
}

const _previewSegments = [
  TranscriptSegment(
    text: '밥은 먹었나? 요즘 날이 추워서 걱정이다.',
    startMs: 4000,
    endMs: 11000,
    speaker: '엄마',
  ),
  TranscriptSegment(
    text: '주말에 내려오면 김치 좀 싸 줄게.',
    startMs: 11000,
    endMs: 18000,
    speaker: '엄마',
  ),
  TranscriptSegment(
    text: '아빠가 너 보고 싶다 카더라.',
    startMs: 18000,
    endMs: 26000,
    speaker: '엄마',
  ),
  TranscriptSegment(
    text: '감기 조심하고 단디 챙겨 입어라.',
    startMs: 26000,
    endMs: 33000,
    speaker: '엄마',
    uncertain: ['단디'],
  ),
];

/// Session-owned, metadata-only preview. Never sends or retains file contents.
class PreviewRecordService extends RecordService {
  final _deletions = <String, DeletionJob>{};
  @override
  bool get canDeleteAccount => true;
  @override
  bool get canRetryDeletion => true;
  @override
  Future<DeletionJob> deleteRecord(String recordId) async {
    final existing = _deletions.values.where(
      (j) => j.targetType == 'record' && j.targetId == recordId,
    );
    if (existing.isNotEmpty) return existing.first;
    if (!_entries.containsKey(recordId)) throw StateError('Record missing');
    return _deletions['preview-delete-$recordId'] = DeletionJob(
      id: 'preview-delete-$recordId',
      targetId: recordId,
      targetType: 'record',
      status: 'pending',
      remaining: const ['storage', 'database', 'external_index', 'adapter'],
    );
  }

  @override
  Future<DeletionJob> deleteAccount() async => _deletions.putIfAbsent(
    'preview-account-delete',
    () => const DeletionJob(
      id: 'preview-account-delete',
      targetId: 'preview-account',
      targetType: 'account',
      status: 'pending',
      remaining: ['storage', 'database', 'external_index', 'adapter', 'auth'],
    ),
  );
  @override
  Future<List<DeletionJob>> deletionJobs() async => _deletions.values.toList();
  @override
  Future<DeletionJob> deletionJob(String jobId) async {
    final job = _deletions[jobId];
    if (job == null) throw StateError('Job missing');
    if (job.targetType == 'account') {
      _entries.clear();
      _versions.clear();
    } else {
      _entries.remove(job.targetId);
      _versions.remove(job.targetId);
    }
    return _deletions[jobId] = DeletionJob(
      id: job.id,
      targetId: job.targetId,
      targetType: job.targetType,
      status: 'completed',
      remaining: const [],
    );
  }

  @override
  Future<DeletionJob> retryDeletion(DeletionJob job) => deletionJob(job.id);
  final _entries = <String, RecordEntry>{};
  final _versions = <String, TextVersion>{};
  @override
  bool get canRetryProcessing => true;
  @override
  Future<void> retryProcessing(String id) async {
    if (!_entries.containsKey(id)) throw StateError('Record missing');
    // Only synthetic preview processing; never invokes STT.
    await Future<void>.delayed(const Duration(milliseconds: 200));
    final entry = _entries[id]!;
    _entries[id] = RecordEntry(
      id: entry.id,
      name: entry.name,
      kind: entry.kind,
      status: RecordStatus.ready,
      createdAt: entry.createdAt,
      sizeBytes: entry.sizeBytes,
    );
  }

  @override
  bool get canPlayOriginal => true;
  @override
  Future<RecordAudioSource> originalAudio(String recordId) async {
    final entry = _entries[recordId];
    if (entry == null || entry.kind != RecordKind.audio) {
      throw StateError('No audio');
    }
    // A silent clock; the preview never keeps or plays the picked file.
    return const RecordAudioSource(
      simulated: true,
      duration: Duration(seconds: 192),
    );
  }

  @override
  Future<RecordReview> review(String recordId) async {
    if (_deletions.values.any(
      (j) => j.targetType == 'account' || j.targetId == recordId,
    )) {
      throw StateError('Deleting');
    }
    final record = _entries[recordId];
    if (record == null) throw StateError('Record missing');
    final version = _versions.putIfAbsent(recordId, () {
      final audio = record.kind == RecordKind.audio;
      final segments = audio
          ? _previewSegments
          : [for (final s in _previewSegments) TranscriptSegment(text: s.text)];
      return TextVersion(
        id: 'preview-$recordId',
        recordId: recordId,
        revision: 1,
        content: segments.map((s) => s.text).join('\n'),
        status: 'draft',
        updatedAt: DateTime.now().toUtc(),
        segments: audio ? segments : null,
      );
    });
    return RecordReview(
      record: record,
      version: version,
      profile: version.status == 'confirmed'
          ? ReviewProfileState.reflected
          : record.personalization,
    );
  }

  @override
  Future<TextVersion> saveText(TextVersion version, String content) async {
    if (_deletions.values.any(
      (j) => j.targetType == 'account' || j.targetId == version.recordId,
    )) {
      throw StateError('Deleting');
    }
    final current = _versions[version.recordId];
    if (current == null ||
        current.status != 'draft' ||
        current.updatedAt != version.updatedAt) {
      throw const BackendFailure(BackendFailureKind.generic, code: 'CONFLICT');
    }
    if (content.trim().isEmpty) throw StateError('Empty text');
    return _versions[version.recordId] = TextVersion(
      id: version.id,
      recordId: version.recordId,
      revision: version.revision,
      content: content,
      status: 'draft',
      updatedAt: DateTime.now().toUtc(),
      segments: current.segments == null
          ? null
          : TranscriptDocument.fromContent(
              content,
              base: current.segments,
            ).segments,
    );
  }

  @override
  Future<TextVersion> confirmText(TextVersion version) async {
    if (_deletions.values.any(
      (j) => j.targetType == 'account' || j.targetId == version.recordId,
    )) {
      throw StateError('Deleting');
    }
    final current = _versions[version.recordId];
    if (current == null) throw StateError('Record missing');
    if (current.status == 'confirmed') return current;
    if (current.updatedAt != version.updatedAt) {
      throw const BackendFailure(BackendFailureKind.generic, code: 'CONFLICT');
    }
    final now = DateTime.now().toUtc();
    final entry = _entries[version.recordId]!;
    _entries[entry.id] = RecordEntry(
      id: entry.id,
      name: entry.name,
      kind: entry.kind,
      status: entry.status,
      createdAt: entry.createdAt,
      sizeBytes: entry.sizeBytes,
      confirmed: true,
      textConfirmed: true,
      confirmedAt: now,
    );
    return _versions[entry.id] = TextVersion(
      id: current.id,
      recordId: entry.id,
      revision: current.revision,
      content: current.content,
      status: 'confirmed',
      updatedAt: now,
      confirmedAt: now,
      segments: current.segments,
    );
  }

  @override
  bool get isPreview => true;
  @override
  RecordPolicy get policy => const RecordPolicy(
    maxBytes: 25 * 1024 * 1024,
    consentVersion: 'preview-personalization-v1',
  );
  @override
  Future<List<RecordEntry>> list() async => _entries.values
      .where((entry) => !_deletions.values.any((j) => j.targetId == entry.id))
      .toList();
  @override
  Future<void> upload(
    RecordUpload upload,
    void Function() onStageChanged,
  ) async {
    final validation = policy.validate(upload.file);
    if (validation != null) throw StateError(validation);
    upload.cancelToken.throwIfCancelled();
    for (final stage in [
      UploadStage.transfer,
      UploadStage.complete,
      UploadStage.done,
    ]) {
      if (upload.stage.index >= stage.index) continue;
      if (stage == UploadStage.complete) {
        // Synthetic progress only: no bytes are read or sent in the preview.
        upload.reportProgress(0);
        for (var step = 1; step <= 7; step++) {
          await Future<void>.delayed(const Duration(milliseconds: 50));
          upload.cancelToken.throwIfCancelled();
          upload.reportProgress(upload.file.sizeBytes * step ~/ 7);
        }
        upload.stage = stage;
        onStageChanged();
        continue;
      }
      await Future<void>.delayed(const Duration(milliseconds: 350));
      upload.cancelToken.throwIfCancelled();
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
  bool get canDeleteAccount => delegate.canDeleteAccount;
  @override
  bool get canRetryDeletion => delegate.canRetryDeletion;
  @override
  Future<DeletionJob> deleteRecord(String recordId) =>
      backend.run(() => delegate.deleteRecord(recordId));
  @override
  Future<DeletionJob> deleteAccount() => backend.run(delegate.deleteAccount);
  @override
  Future<DeletionJob> deletionJob(String jobId) =>
      backend.run(() => delegate.deletionJob(jobId));
  @override
  Future<List<DeletionJob>> deletionJobs() =>
      backend.run(delegate.deletionJobs);
  @override
  Future<DeletionJob> retryDeletion(DeletionJob job) =>
      backend.run(() => delegate.retryDeletion(job));
  @override
  Future<RecordReview> review(String recordId) =>
      backend.run(() => delegate.review(recordId));
  @override
  Future<TextVersion> saveText(TextVersion version, String content) =>
      backend.run(() => delegate.saveText(version, content));
  @override
  Future<TextVersion> confirmText(TextVersion version) =>
      backend.run(() => delegate.confirmText(version));
  @override
  bool get isPreview => delegate.isPreview;
  @override
  RecordPolicy get policy => delegate.policy;
  @override
  bool get canRetryProcessing => delegate.canRetryProcessing;
  @override
  bool get canPlayOriginal => delegate.canPlayOriginal;
  @override
  Future<RecordAudioSource> originalAudio(String recordId) =>
      backend.run(() => delegate.originalAudio(recordId));
  @override
  Future<List<RecordEntry>> list() => backend.run(delegate.list);
  @override
  Future<void> upload(RecordUpload upload, void Function() onStageChanged) =>
      backend.run(() => delegate.upload(upload, onStageChanged));
  @override
  Future<DeletionJob?> discardUpload(RecordUpload upload) =>
      backend.run(() => delegate.discardUpload(upload));
  @override
  Future<void> retryProcessing(String id) =>
      backend.run(() => delegate.retryProcessing(id));
}
