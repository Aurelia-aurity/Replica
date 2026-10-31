import 'package:flutter/foundation.dart';

import '../services/backend_service.dart';
import 'record_models.dart';

class TextVersion {
  const TextVersion({
    required this.id,
    required this.recordId,
    required this.revision,
    required this.content,
    required this.status,
    required this.updatedAt,
    this.confirmedAt,
  });
  final String id, recordId, content, status;
  final int revision;
  final DateTime updatedAt;
  final DateTime? confirmedAt;
  factory TextVersion.fromJson(Map<String, dynamic> json) => TextVersion(
    id: json['id'] as String,
    recordId: json['record_id'] as String,
    revision: json['revision'] as int,
    content: json['content'] as String,
    status: json['status'] as String,
    updatedAt: DateTime.parse(json['updated_at'] as String),
    confirmedAt: json['confirmed_at'] == null
        ? null
        : DateTime.parse(json['confirmed_at'] as String),
  );
}

enum ReviewProfileState { unknown, available, processing, reflected, failed }

class RecordReview {
  const RecordReview({
    required this.record,
    this.version,
    this.profile = ReviewProfileState.unknown,
  });
  final RecordEntry record;
  final TextVersion? version;
  final ReviewProfileState profile;
}

abstract class RecordReviewService {
  Future<RecordReview> review(String recordId);
  Future<TextVersion> saveText(TextVersion version, String content);
  Future<TextVersion> confirmText(TextVersion version);
}

/// One screen owns requests; the session-owned map preserves unsent edits.
class RecordReviewController extends ChangeNotifier {
  RecordReviewController({
    required this.service,
    required this.recordId,
    required this.drafts,
  });
  final RecordReviewService service;
  final String recordId;
  final Map<String, String> drafts;
  RecordReview? value;
  String content = '';
  bool loading = false, busy = false, disposed = false, conflict = false;
  String? error;
  bool get confirmed => value?.version?.status == 'confirmed';
  bool get canConfirm =>
      !loading &&
      !busy &&
      !conflict &&
      !confirmed &&
      value?.version?.status == 'draft' &&
      content.trim().isNotEmpty &&
      value?.record.status == RecordStatus.ready;
  void changed() {
    if (!disposed) notifyListeners();
  }

  @override
  void dispose() {
    disposed = true;
    super.dispose();
  }

  void edit(String text) {
    if (busy || confirmed) return;
    content = text;
    drafts[recordId] = text;
    changed();
  }

  Future<void> load() async {
    if (loading || busy) return;
    loading = true;
    error = null;
    changed();
    try {
      final result = await service.review(recordId);
      if (disposed) return;
      value = result;
      content = result.version?.status == 'confirmed'
          ? result.version!.content
          : drafts[recordId] ?? result.version?.content ?? '';
      conflict = false;
    } catch (_) {
      error = '전사문을 불러오지 못했어요. 다시 시도해 주세요.';
    } finally {
      loading = false;
      changed();
    }
  }

  Future<bool> save({bool confirm = false}) async {
    if (confirm && !canConfirm) return false;
    if (busy ||
        loading ||
        conflict ||
        confirmed ||
        value?.version == null ||
        content.trim().isEmpty) {
      return false;
    }
    busy = true;
    error = null;
    changed();
    try {
      var version = value!.version!;
      if (content != version.content) {
        version = await service.saveText(version, content);
      }
      if (disposed) return false;
      value = RecordReview(
        record: value!.record,
        version: version,
        profile: value!.profile,
      );
      if (confirm) {
        version = await service.confirmText(version);
        if (disposed) return false;
        if (version.status != 'confirmed' || version.confirmedAt == null) {
          throw StateError('Invalid confirmation');
        }
        value = RecordReview(record: value!.record, version: version);
      }
      drafts.remove(recordId);
      return true;
    } catch (e) {
      if (e is BackendFailure && e.code == 'CONFLICT') conflict = true;
      error = conflict
          ? '다른 곳에서 전사문이 바뀌었어요. 입력은 보관했으니 최신 내용을 다시 확인해 주세요.'
          : '저장하지 못했어요. 수정 내용은 유지돼요. 다시 시도해 주세요.';
      return false;
    } finally {
      busy = false;
      changed();
    }
  }
}
