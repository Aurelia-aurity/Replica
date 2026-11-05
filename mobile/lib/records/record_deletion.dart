import 'dart:async';

import 'package:flutter/foundation.dart';

class DeletionJob {
  const DeletionJob({
    required this.id,
    required this.targetId,
    required this.targetType,
    required this.status,
    required this.remaining,
    this.attemptCount,
    this.lastErrorCode,
  });
  final String id, targetId, targetType, status;
  final List<String> remaining;
  final int? attemptCount;

  /// Safe failure code of the last attempt (never a raw server message).
  final String? lastErrorCode;
  bool get complete => status == 'completed' && remaining.isEmpty;
  bool get failed => status == 'failed' || (status == 'completed' && !complete);

  /// Still being worked on by the server; checking again may change the state.
  bool get running => !complete && !failed;

  factory DeletionJob.fromJson(Map<String, dynamic> json) => DeletionJob(
    id: json['id'] as String,
    targetId: json['target_id'] as String,
    targetType: json['target_type'] as String,
    status: json['status'] as String,
    remaining: (json['remaining_steps'] as List).cast<String>(),
    attemptCount: json['attempt_count'] as int?,
    lastErrorCode: json['last_error_code'] as String?,
  );

  /// Steps this kind of deletion goes through, in order.
  List<String> get steps => [
    'storage',
    'database',
    'external_index',
    'adapter',
    if (targetType == 'account') 'auth',
  ];

  /// Steps that are finished (everything in scope that is no longer pending).
  List<String> get doneSteps =>
      steps.where((step) => !remaining.contains(step)).toList();

  static String stepLabel(String step) => switch (step) {
    'storage' => '원본 파일',
    'database' => '기록·전사문',
    'external_index' => '검색 색인',
    'adapter' => '개인화 산출물',
    'auth' => '계정 정보',
    _ => '기타 자료',
  };

  String get remainingLabel => remaining.map(stepLabel).join(' · ');
}

abstract class RecordDeletionService {
  bool get canDeleteAccount;
  bool get canRetryDeletion;
  Future<DeletionJob> deleteRecord(String recordId);
  Future<DeletionJob> deleteAccount();
  Future<DeletionJob> deletionJob(String jobId);
  Future<List<DeletionJob>> deletionJobs();
  Future<DeletionJob> retryDeletion(DeletionJob job);
}

class RecordDeletionController extends ChangeNotifier {
  RecordDeletionController({
    required this.service,
    required this.targetId,
    this.account = false,
    this.job,
  });
  final RecordDeletionService service;
  final String targetId;
  final bool account;
  DeletionJob? job;
  bool busy = false, disposed = false;
  String? error;
  String? label;
  Timer? _watch;
  int _watchChecks = 0;
  bool get complete => job?.complete ?? false;
  void changed() {
    if (!disposed) notifyListeners();
  }

  /// Checks a running job again every [interval], at most [maxChecks] times.
  /// A failed job is never re-checked on its own; the user decides to retry.
  void watch({
    Duration interval = const Duration(seconds: 3),
    int maxChecks = 20,
  }) {
    _watch?.cancel();
    _watchChecks = 0;
    void schedule() {
      if (disposed || _watchChecks >= maxChecks) return;
      final current = job;
      if (current == null || !current.running) return;
      _watch = Timer(interval, () async {
        _watchChecks++;
        if (!busy && !disposed) await execute();
        schedule();
      });
    }

    schedule();
  }

  void stopWatching() {
    _watch?.cancel();
    _watch = null;
  }

  @override
  void dispose() {
    disposed = true;
    stopWatching();
    super.dispose();
  }

  Future<bool> execute({bool retry = false}) async {
    if (job == null && account && !service.canDeleteAccount) {
      error = '계정 전체 삭제는 아직 연결되지 않았어요.';
      changed();
      return false;
    }
    if (busy || complete) return complete;
    busy = true;
    error = null;
    changed();
    try {
      final previous = job;
      final result = previous == null
          ? await (account
                ? service.deleteAccount()
                : service.deleteRecord(targetId))
          : retry && service.canRetryDeletion
          ? await service.retryDeletion(previous)
          : await service.deletionJob(previous.id);
      if (disposed) return false;
      if (result.targetType != (account ? 'account' : 'record') ||
          (!account && result.targetId != targetId) ||
          (previous != null &&
              (result.id != previous.id ||
                  result.targetId != previous.targetId))) {
        throw StateError('Deletion target mismatch');
      }
      job = result;
      return complete;
    } catch (_) {
      error = '삭제 상태를 확인하지 못했어요. 완료로 처리하지 않았으니 다시 확인해 주세요.';
      return false;
    } finally {
      busy = false;
      changed();
    }
  }
}
