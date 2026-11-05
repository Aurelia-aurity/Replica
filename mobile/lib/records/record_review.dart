import 'package:flutter/foundation.dart';

import '../services/backend_service.dart';
import 'record_models.dart';
import 'transcript_segments.dart';

export 'record_models.dart' show ReviewProfileState;

class TextVersion {
  const TextVersion({
    required this.id,
    required this.recordId,
    required this.revision,
    required this.content,
    required this.status,
    required this.updatedAt,
    this.confirmedAt,
    this.segments,
  });
  final String id, recordId, content, status;
  final int revision;
  final DateTime updatedAt;
  final DateTime? confirmedAt;

  /// Optional timed segments. Present only when they reproduce [content].
  final List<TranscriptSegment>? segments;
  factory TextVersion.fromJson(Map<String, dynamic> json) {
    final content = json['content'] as String;
    final raw = json['segments'];
    return TextVersion(
      id: json['id'] as String,
      recordId: json['record_id'] as String,
      revision: json['revision'] as int,
      content: content,
      status: json['status'] as String,
      updatedAt: DateTime.parse(json['updated_at'] as String),
      confirmedAt: json['confirmed_at'] == null
          ? null
          : DateTime.parse(json['confirmed_at'] as String),
      segments: raw is List
          ? TranscriptDocument.validated([
              for (final item in raw) ?TranscriptSegment.tryParse(item),
            ], content)
          : null,
    );
  }
}

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

/// Session-owned edits and the server version the user edited or accepted.
class RecordReviewDraft {
  const RecordReviewDraft({required this.content, this.baseVersion});

  final String content;
  final TextVersion? baseVersion;
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
  final Map<String, RecordReviewDraft> drafts;
  RecordReview? value;
  String content = '';
  bool loading = false, busy = false, disposed = false, conflict = false;

  /// The server text changed under an unsaved edit. The user must choose
  /// between their edit and the server text before saving or confirming.
  bool needsResolution = false;
  String? error;
  bool get confirmed => value?.version?.status == 'confirmed';
  bool get canConfirm =>
      !loading &&
      !busy &&
      !conflict &&
      !needsResolution &&
      !confirmed &&
      value?.version?.status == 'draft' &&
      content.trim().isNotEmpty &&
      value?.record.status == RecordStatus.ready;

  /// Segment view of the text being edited (timing follows the server base).
  TranscriptDocument get document =>
      TranscriptDocument.fromContent(content, base: value?.version?.segments);

  void changed() {
    if (!disposed) notifyListeners();
  }

  @override
  void dispose() {
    disposed = true;
    super.dispose();
  }

  void edit(String text) {
    if (busy || confirmed || needsResolution) return;
    content = text;
    drafts[recordId] = RecordReviewDraft(
      content: text,
      baseVersion: drafts[recordId]?.baseVersion ?? value?.version,
    );
    changed();
  }

  /// Replaces one segment. Line breaks are folded so the segment count — and
  /// with it the timing metadata — stays stable while editing.
  void editSegment(int index, String text) {
    final segments = document.segments;
    if (index < 0 || index >= segments.length) return;
    final folded = text.replaceAll(RegExp(r'[\r\n]+'), ' ');
    edit(
      [
        for (var i = 0; i < segments.length; i++)
          i == index ? folded : segments[i].text,
      ].join('\n'),
    );
  }

  Future<void> load({bool quiet = false}) async {
    if (loading || busy) return;
    if (!quiet) {
      loading = true;
      error = null;
      changed();
    }
    try {
      final result = await service.review(recordId);
      if (disposed) return;
      final latest = result.version;
      final edited = drafts[recordId];
      final base = edited?.baseVersion;
      value = result;
      content = latest?.status == 'confirmed'
          ? latest!.content
          : edited?.content ?? latest?.content ?? '';
      // Compare with the session-owned base on every load, including a newly
      // opened screen. Keep the old base until the user resolves the conflict.
      needsResolution =
          latest != null &&
          latest.status == 'draft' &&
          edited != null &&
          edited.content != latest.content &&
          (base == null ||
              base.id != latest.id ||
              base.revision != latest.revision ||
              base.updatedAt != latest.updatedAt ||
              base.content != latest.content);
      if (edited != null &&
          latest != null &&
          !needsResolution &&
          latest.status == 'draft') {
        drafts[recordId] = RecordReviewDraft(
          content: content,
          baseVersion: latest,
        );
      }
      conflict = false;
      if (!quiet) error = null;
    } catch (_) {
      if (!quiet) error = '전사문을 불러오지 못했어요. 다시 시도해 주세요.';
    } finally {
      if (!quiet) loading = false;
      changed();
    }
  }

  /// Keeps the user's edit and saves it over the newer server text.
  void keepMine() {
    if (!needsResolution) return;
    drafts[recordId] = RecordReviewDraft(
      content: content,
      baseVersion: value!.version!,
    );
    needsResolution = false;
    changed();
  }

  /// Drops the user's edit and adopts the server text.
  void useServer() {
    if (!needsResolution) return;
    final latest = value?.version;
    drafts.remove(recordId);
    content = latest?.content ?? '';
    needsResolution = false;
    changed();
  }

  Future<bool> save({bool confirm = false}) async {
    if (confirm && !canConfirm) return false;
    if (busy ||
        loading ||
        conflict ||
        needsResolution ||
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
      // A later confirmation can fail independently of this successful save.
      // Reopening must compare against the version we just saved.
      drafts[recordId] = RecordReviewDraft(
        content: content,
        baseVersion: version,
      );
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
        value = RecordReview(
          record: value!.record,
          version: version,
          // Confirmation proves the text is saved, not profile/index readiness.
          profile: ReviewProfileState.unknown,
        );
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
