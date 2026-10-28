import 'package:flutter/foundation.dart';

import '../services/backend_service.dart';
import 'record_models.dart';
import 'record_service.dart';

class RecordController extends ChangeNotifier {
  RecordController(this.service);
  final RecordService service;
  List<RecordEntry> entries = [];
  bool loading = false;
  String? listError;
  RecordFile? selected;
  String? selectionError;
  bool consent = false;
  bool uploading = false;
  String? uploadError;
  bool uploadRetryAllowed = true;
  bool uploadSucceeded = false;
  RecordUpload? attempt;
  final retrying = <String>{};
  String? actionError;
  bool _disposed = false;

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }

  Future<void> refresh() async {
    if (loading) return;
    loading = true;
    listError = null;
    _changed();
    try {
      final result = await service.list();
      if (_disposed) return;
      entries = result.where((e) => e.status != RecordStatus.deleting).toList()
        ..sort((a, b) {
          final date = b.createdAt.compareTo(a.createdAt);
          return date != 0 ? date : b.id.compareTo(a.id);
        });
    } catch (_) {
      listError = '기록을 불러오지 못했어요. 다시 시도해 주세요.';
    } finally {
      loading = false;
      _changed();
    }
  }

  void beginSelection() {
    if (uploading) return;
    selected = null;
    consent = false;
    attempt = null;
    uploadError = null;
    uploadRetryAllowed = true;
    selectionError = null;
    uploadSucceeded = false;
    _changed();
  }

  void select(RecordFile file) {
    if (uploading || attempt != null) return;
    selectionError = service.policy.validate(file);
    selected = selectionError == null ? file : null;
    consent = false;
    uploadError = null;
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
      !uploadSucceeded &&
      uploadRetryAllowed;
  Future<void> send() async {
    if (!canUpload) return;
    final validation = service.policy.validate(selected!);
    if (validation != null) {
      selectionError = validation;
      _changed();
      return;
    }
    attempt ??= RecordUpload(
      file: selected!,
      consentAt: DateTime.now(),
      consentVersion: service.policy.consentVersion,
    );
    uploading = true;
    uploadError = null;
    _changed();
    try {
      await service.upload(attempt!, _changed);
      if (_disposed) return;
      uploadSucceeded = true;
      await refresh();
    } catch (error) {
      uploadRetryAllowed = error is! BackendFailure || error.retryable;
      uploadError = error is BackendFailure && !error.retryable
          ? '업로드 상태를 확인할 수 없어요. 목록을 새로고침해 주세요.'
          : '업로드하지 못했어요. 같은 기록으로 다시 시도할 수 있어요.';
    } finally {
      uploading = false;
      _changed();
    }
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
