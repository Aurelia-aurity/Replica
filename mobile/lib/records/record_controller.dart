import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';

import '../services/backend_service.dart';
import 'record_deletion.dart';
import 'record_models.dart';
import 'record_review.dart';
import 'record_service.dart';
import 'text_analysis.dart';

class RecordController extends ChangeNotifier {
  RecordController(
    this.service, {
    this.pollInterval = const Duration(seconds: 5),
    this.analyze = analyzeTextRecord,
  });
  final RecordService service;

  /// Base delay between status checks while a record is still being processed.
  final Duration pollInterval;
  final Future<TextAnalysis> Function(RecordFile file) analyze;
  List<RecordEntry> entries = [];
  final reviewDrafts = <String, RecordReviewDraft>{};
  final deletions = <String, RecordDeletionController>{};
  String? deletionLoadError;
  RecordDeletionController deletionFor(
    String recordId, {
    bool account = false,
  }) {
    final key = account ? '#account' : recordId;
    return deletions.putIfAbsent(key, () {
      final controller = RecordDeletionController(
        service: service,
        targetId: key,
        account: account,
      );
      controller.addListener(_deletionChanged);
      return controller;
    });
  }

  void _deletionChanged() {
    if (_disposed) return;
    for (final controller in deletions.values) {
      if (controller.job == null) continue;
      if (controller.account && controller.complete) {
        clearLocalData();
      }
      if (!controller.account) {
        entries.removeWhere((entry) => entry.id == controller.targetId);
        if (controller.complete) reviewDrafts.remove(controller.targetId);
      }
    }
    _changed();
  }

  /// Drops everything this session holds about the user's records: the list,
  /// unsent transcript edits, the picked file and the parsed speaker names.
  void clearLocalData() {
    // Detach progress callbacks before stopping the session-owned transfer.
    attempt?.onProgress = null;
    attempt?.cancelToken.cancel();
    entries.clear();
    reviewDrafts.clear();
    selected = null;
    // Invalidates an analysis still reading the file so its late result
    // cannot bring the speaker names back.
    _resetAnalysis();
    attempt = null;
    consent = false;
  }

  Future<void> syncDeletionJobs() async {
    deletionLoadError = null;
    try {
      final jobs = await service.deletionJobs();
      if (_disposed) return;
      for (final job in jobs.where(
        (j) => j.targetType == 'record' || j.targetType == 'account',
      )) {
        final controller = deletionFor(
          job.targetId,
          account: job.targetType == 'account',
        );
        if (!controller.busy) controller.job = job;
      }
      _deletionChanged();
    } catch (_) {
      deletionLoadError = '삭제 요청 목록을 불러오지 못했어요. 새로고침해 주세요.';
    }
    _changed();
  }

  bool loading = false;
  String? listError;
  RecordFile? selected;
  String? selectionError;
  bool consent = false;
  bool uploading = false;
  bool cancelling = false;
  String? uploadError;
  String? uploadNotice;
  bool uploadRetryAllowed = true;
  bool uploadSucceeded = false;
  RecordUpload? attempt;
  final retrying = <String>{};
  String? actionError;
  bool _disposed = false;

  // SC-04: what the picked text file turned out to be, and who is chosen.
  TextAnalysis? analysis;
  bool analyzing = false;
  String? analysisError;
  String? targetSpeaker;
  String? providerSpeaker;
  Future<void>? _analysis;
  int _analysisToken = 0;

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    stopPolling();
    for (final deletion in deletions.values) {
      deletion.removeListener(_deletionChanged);
      deletion.dispose();
    }
    clearLocalData();
    super.dispose();
  }

  void _apply(List<RecordEntry> result) {
    entries = result.where((e) => e.status != RecordStatus.deleting).toList()
      ..sort((a, b) {
        final date = b.createdAt.compareTo(a.createdAt);
        return date != 0 ? date : b.id.compareTo(a.id);
      });
  }

  Future<void> refresh() async {
    if (loading) return;
    loading = true;
    listError = null;
    _changed();
    try {
      final result = await service.list();
      if (_disposed) return;
      _apply(result);
      await syncDeletionJobs();
    } catch (_) {
      listError = '기록을 불러오지 못했어요. 다시 시도해 주세요.';
    } finally {
      loading = false;
      _changed();
      _schedulePoll();
    }
  }

  // --- Status polling -------------------------------------------------------

  Timer? _pollTimer;
  bool _pollEnabled = false;
  int _pollFailures = 0;

  /// True while some record is uploading, being transcribed or being reflected.
  bool get hasPending => entries.any((e) => e.inProgress);

  /// Starts quiet status checks while records are being processed. The list
  /// screen turns this on while it is visible and off when it goes away.
  void startPolling() {
    _pollEnabled = true;
    _schedulePoll();
  }

  void stopPolling() {
    _pollEnabled = false;
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  void _schedulePoll() {
    _pollTimer?.cancel();
    _pollTimer = null;
    if (!_pollEnabled || _disposed || !hasPending) return;
    // Back off after failures: 1x, 2x, 4x … up to 8x the base interval.
    final factor = 1 << min(_pollFailures, 3);
    _pollTimer = Timer(pollInterval * factor, _poll);
  }

  Future<void> _poll() async {
    if (_disposed || !_pollEnabled) return;
    if (loading || uploading) return _schedulePoll();
    try {
      final result = await service.list();
      if (_disposed) return;
      _apply(result);
      _pollFailures = 0;
      _changed();
    } on BackendFailure catch (failure) {
      // A dead session is handled by the session layer; stop asking.
      if (failure.kind == BackendFailureKind.unauthenticated ||
          failure.kind == BackendFailureKind.staleSession) {
        return stopPolling();
      }
      _pollFailures++;
    } catch (_) {
      _pollFailures++;
    }
    _schedulePoll();
  }

  // --- Selection and analysis ----------------------------------------------

  void beginSelection() {
    if (uploading) return;
    // A failed attempt that is abandoned must not leave a reservation behind.
    final abandoned = attempt;
    if (abandoned != null &&
        !uploadSucceeded &&
        abandoned.reservationRequested) {
      unawaited(_discardQuietly(abandoned));
    }
    selected = null;
    consent = false;
    attempt = null;
    uploadError = null;
    uploadNotice = null;
    uploadRetryAllowed = true;
    selectionError = null;
    uploadSucceeded = false;
    _resetAnalysis();
    _changed();
  }

  void _resetAnalysis() {
    _analysisToken++;
    analysis = null;
    analyzing = false;
    analysisError = null;
    targetSpeaker = null;
    providerSpeaker = null;
    _analysis = null;
  }

  void select(RecordFile file) {
    if (uploading || attempt != null) return;
    selectionError = service.policy.validate(file);
    selected = selectionError == null ? file : null;
    consent = false;
    uploadError = null;
    uploadNotice = null;
    _resetAnalysis();
    if (selected != null && file.kind == RecordKind.text) {
      final token = _analysisToken;
      analyzing = true;
      _analysis = _analyze(file, token);
    }
    _changed();
  }

  Future<void> _analyze(RecordFile file, int token) async {
    TextAnalysis? result;
    var failed = false;
    try {
      result = await analyze(file);
    } catch (_) {
      failed = true;
    }
    if (_disposed || token != _analysisToken) return;
    analyzing = false;
    if (failed) {
      // The upload does not depend on the speaker step; fall back to the file.
      analysis = TextAnalysis.plain;
      analysisError = '파일 내용을 확인하지 못해 화자 선택 없이 올려요.';
    } else {
      analysis = result;
    }
    _changed();
  }

  bool get needsSpeakers => analysis?.needsSpeakerSelection ?? false;

  void selectTarget(String? name) {
    if (uploading || attempt != null || name == providerSpeaker) return;
    targetSpeaker = name;
    _changed();
  }

  void selectProvider(String? name) {
    if (uploading || attempt != null || name == targetSpeaker) return;
    providerSpeaker = name;
    _changed();
  }

  void setConsent(bool value) {
    if (uploading || attempt != null) return;
    consent = value;
    _changed();
  }

  bool get canUpload =>
      selected != null &&
      consent &&
      !uploading &&
      !analyzing &&
      !uploadSucceeded &&
      uploadRetryAllowed &&
      (!needsSpeakers || (targetSpeaker != null && providerSpeaker != null));

  /// Cancelling is possible until the file is fully transferred.
  bool get canCancelUpload =>
      uploading &&
      !cancelling &&
      (attempt?.stage == UploadStage.reserve ||
          attempt?.stage == UploadStage.transfer);

  Future<void> send() async {
    // Let the speaker lookup of the just-picked file finish first.
    if (_analysis != null) await _analysis;
    if (_disposed || !canUpload) return;
    final validation = service.policy.validate(selected!);
    if (validation != null) {
      selectionError = validation;
      _changed();
      return;
    }
    final current = attempt ??= RecordUpload(
      file: selected!,
      consentAt: DateTime.now(),
      consentVersion: service.policy.consentVersion,
      targetSpeaker: needsSpeakers ? targetSpeaker : null,
      providerSpeaker: needsSpeakers ? providerSpeaker : null,
    );
    current.onProgress = _changed;
    uploading = true;
    uploadError = null;
    uploadNotice = null;
    _changed();
    try {
      await service.upload(current, _changed);
      if (_disposed) return;
      uploadSucceeded = true;
      await refresh();
    } on UploadCancelled {
      if (_disposed) return;
      await _afterCancel(current);
    } catch (error) {
      if (_disposed) return;
      if (current.cancelToken.isCancelled) {
        await _afterCancel(current);
      } else {
        uploadRetryAllowed = error is! BackendFailure || error.retryable;
        uploadError = _uploadMessage(error);
      }
    } finally {
      current.onProgress = null;
      uploading = false;
      cancelling = false;
      _changed();
    }
  }

  /// Says why the upload stopped, without exposing raw server text.
  String _uploadMessage(Object error) {
    if (error is BackendFailure) {
      if (error.code == 'FILE_CHANGED') {
        return '올리는 도중 파일이 바뀌었어요. 파일을 다시 선택해 주세요.';
      }
      if (error.kind == BackendFailureKind.forbidden) {
        return '이 기록을 올릴 권한이 없어요.';
      }
      if (error.kind == BackendFailureKind.network) {
        return '네트워크 연결이 불안정해요. 같은 기록으로 다시 시도할 수 있어요.';
      }
      if (!error.retryable) {
        return '업로드 상태를 확인할 수 없어요. 목록을 새로고침해 주세요.';
      }
    }
    return '업로드하지 못했어요. 같은 기록으로 다시 시도할 수 있어요.';
  }

  Future<DeletionJob?> _discardUpload(RecordUpload upload) async {
    try {
      final job = await service.discardUpload(upload);
      if (_disposed || job == null) return null;
      if (job.targetType != 'record' || job.targetId != upload.id) {
        throw StateError('Deletion target mismatch');
      }
      final deletion = deletionFor(upload.id);
      deletion.label = upload.file.name;
      deletion.job = job;
      deletion.error = null;
      deletion.changed();
      return job;
    } catch (_) {
      if (!_disposed) {
        final deletion = deletionFor(upload.id);
        deletion.label = upload.file.name;
        deletion.error = '임시 기록의 삭제 상태를 확인하지 못했어요. 다시 확인해 주세요.';
        deletion.changed();
      }
      rethrow;
    }
  }

  Future<void> _discardQuietly(RecordUpload old) async {
    try {
      await _discardUpload(old);
    } catch (_) {
      /* The deletion card retains the error for the user to check. */
    }
  }

  /// Stops the running upload and requests cleanup. The picked file stays so
  /// the user can upload again after confirming consent.
  void cancelUpload() {
    final current = attempt;
    if (!canCancelUpload || current == null) return;
    cancelling = true;
    current.cancelToken.cancel();
    _changed();
  }

  Future<void> _afterCancel(RecordUpload current) async {
    uploadNotice = '업로드를 취소했어요. 임시 기록을 정리하고 있어요.';
    try {
      final job = await _discardUpload(current);
      if (_disposed) return;
      uploadNotice = job == null
          ? '업로드를 취소했어요.'
          : job.complete
          ? '업로드를 취소하고 임시 기록을 삭제했어요.'
          : job.failed
          ? '업로드를 취소했지만 일부 자료를 삭제하지 못했어요. 기록 목록에서 삭제 상태를 확인해 주세요.'
          : '업로드를 취소했어요. 임시 기록 삭제가 진행 중이에요. 기록 목록에서 상태를 확인할 수 있어요.';
    } catch (_) {
      if (_disposed) return;
      uploadNotice = '업로드를 취소했어요. 서버에 남은 임시 기록이 있으면 기록 목록에서 확인해 주세요.';
    }
    if (_disposed) return;
    attempt = null;
    consent = false;
    uploadError = null;
    uploadRetryAllowed = true;
  }

  Future<void> retry(RecordEntry entry) async {
    if (!service.canRetryProcessing || !retrying.add(entry.id)) return;
    actionError = null;
    _changed();
    try {
      await service.retryProcessing(entry.id);
      if (!_disposed) await refresh();
    } catch (_) {
      actionError = '다시 요청하지 못했어요. 잠시 후 다시 시도해 주세요.';
    } finally {
      retrying.remove(entry.id);
      _changed();
    }
  }
}
