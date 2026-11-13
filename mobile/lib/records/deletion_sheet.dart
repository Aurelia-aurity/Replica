import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'record_deletion.dart';

Future<bool?> showDeletionSheet(
  BuildContext context,
  RecordDeletionController controller, {
  bool preview = false,
}) => showModalBottomSheet<bool>(
  context: context,
  isScrollControlled: true,
  isDismissible: false,
  enableDrag: false,
  backgroundColor: Colors.white,
  builder: (_) => DeletionSheet(
    controller: controller,
    preview: preview,
    // The synthetic preview finishes only when the user checks, by design.
    autoCheck: preview ? null : const Duration(seconds: 3),
  ),
);

class DeletionSheet extends StatefulWidget {
  const DeletionSheet({
    super.key,
    required this.controller,
    this.preview = false,
    this.autoCheck,
  });
  final RecordDeletionController controller;
  final bool preview;

  /// While the job is still running, check it again this often (bounded).
  final Duration? autoCheck;
  @override
  State<DeletionSheet> createState() => _DeletionSheetState();
}

class _DeletionSheetState extends State<DeletionSheet> {
  bool _understood = false;
  bool _watching = false;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_onChange);
    _onChange();
  }

  void _onChange() {
    final interval = widget.autoCheck;
    if (interval == null) return;
    final job = widget.controller.job;
    if (job != null && job.running && !_watching) {
      _watching = true;
      widget.controller.watch(interval: interval);
    } else if (job == null || !job.running) {
      _watching = false;
    }
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onChange);
    widget.controller.stopWatching();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    final preview = widget.preview;
    return AnimatedBuilder(
      animation: controller,
      builder: (context, _) {
        final c = controller;
        final job = c.job;
        final failed = job?.failed ?? false;
        final supported =
            job != null || !c.account || c.service.canDeleteAccount;
        final needsConfirmation = c.account && job == null && supported;
        return PopScope(
          canPop: !c.busy,
          child: SafeArea(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: BoxConstraints(
                  maxHeight: MediaQuery.sizeOf(context).height * .85,
                ),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Flexible(
                      child: SingleChildScrollView(
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            Center(
                              child: Container(
                                width: 40,
                                height: 4,
                                decoration: BoxDecoration(
                                  color: replicaBorder,
                                  borderRadius: BorderRadius.circular(8),
                                ),
                              ),
                            ),
                            const SizedBox(height: 24),
                            Text(
                              c.account ? '계정과 모든 자료를 삭제할까요?' : '이 기록을 삭제할까요?',
                              style: const TextStyle(
                                fontSize: 21,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                            const SizedBox(height: 12),
                            Text(
                              c.account
                                  ? '모든 기록과 대화, 계정 정보가 삭제 대상이에요. 완료되면 로그아웃되며 되돌릴 수 없어요.'
                                  : '아래 항목이 함께 지워지고, 되돌릴 수 없어요.',
                              style: const TextStyle(
                                height: 1.6,
                                color: Color(0xFF68766F),
                              ),
                            ),
                            const SizedBox(height: 20),
                            Container(
                              padding: const EdgeInsets.all(16),
                              decoration: BoxDecoration(
                                color: replicaBackground,
                                borderRadius: BorderRadius.circular(16),
                              ),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  for (final item in [
                                    (Icons.graphic_eq, '원본 파일'),
                                    (Icons.description_outlined, '전사문'),
                                    (Icons.search, '대화 검색 색인'),
                                    (
                                      Icons.person_outline,
                                      '말투 프로필에서 이 기록 반영분 (프로필 다시 생성)',
                                    ),
                                    if (c.account)
                                      (
                                        Icons.manage_accounts_outlined,
                                        '모든 대화와 계정 정보',
                                      ),
                                  ])
                                    Padding(
                                      padding: const EdgeInsets.symmetric(
                                        vertical: 8,
                                      ),
                                      child: Row(
                                        crossAxisAlignment:
                                            CrossAxisAlignment.start,
                                        children: [
                                          Icon(
                                            item.$1,
                                            size: 20,
                                            color: const Color(0xFF526159),
                                          ),
                                          const SizedBox(width: 12),
                                          Expanded(
                                            child: Text(
                                              item.$2,
                                              style: const TextStyle(
                                                height: 1.5,
                                              ),
                                            ),
                                          ),
                                        ],
                                      ),
                                    ),
                                ],
                              ),
                            ),
                            if (needsConfirmation)
                              Padding(
                                padding: const EdgeInsets.only(top: 12),
                                child: CheckboxListTile(
                                  key: const ValueKey(
                                    'account-delete-understood',
                                  ),
                                  contentPadding: EdgeInsets.zero,
                                  controlAffinity:
                                      ListTileControlAffinity.leading,
                                  activeColor: const Color(0xFFB3261E),
                                  value: _understood,
                                  onChanged: c.busy
                                      ? null
                                      : (value) => setState(
                                          () => _understood = value ?? false,
                                        ),
                                  title: const Text(
                                    '계정과 모든 자료가 영구 삭제되고 되돌릴 수 없다는 것을 이해했어요.',
                                    style: TextStyle(fontSize: 14, height: 1.5),
                                  ),
                                ),
                              ),
                            if (preview)
                              const Padding(
                                padding: EdgeInsets.only(top: 12),
                                child: Text(
                                  '미리보기 · 실제 계정이나 서버 자료는 삭제하지 않아요.',
                                  style: TextStyle(
                                    fontSize: 12,
                                    color: Color(0xFF68766F),
                                  ),
                                ),
                              ),
                            if (!supported)
                              const Padding(
                                padding: EdgeInsets.only(top: 12),
                                child: Text(
                                  '계정 전체 삭제는 아직 연결되지 않았어요. 현재 화면에서는 요청할 수 없어요.',
                                ),
                              ),
                            if (job != null) ...[
                              const SizedBox(height: 16),
                              Semantics(
                                liveRegion: true,
                                child: Text(
                                  c.complete
                                      ? (preview
                                            ? '미리보기 자료 삭제를 완료했어요.'
                                            : '삭제를 완료했어요.')
                                      : failed
                                      ? '일부 자료를 삭제하지 못했어요.'
                                      : '삭제가 진행 중이에요. 아직 완료되지 않았어요.',
                                  style: TextStyle(
                                    fontWeight: FontWeight.w600,
                                    color: failed
                                        ? const Color(0xFFB3261E)
                                        : replicaGreen,
                                  ),
                                ),
                              ),
                              const SizedBox(height: 8),
                              _steps(job, failed),
                              if (!c.complete && job.remaining.isNotEmpty)
                                Padding(
                                  padding: const EdgeInsets.only(top: 8),
                                  child: Text(
                                    '남은 항목: ${job.remainingLabel}',
                                    style: const TextStyle(height: 1.6),
                                  ),
                                ),
                              if (failed &&
                                  ((job.attemptCount ?? 0) > 1 ||
                                      job.lastErrorCode != null))
                                Padding(
                                  padding: const EdgeInsets.only(top: 4),
                                  child: Text(
                                    [
                                      if ((job.attemptCount ?? 0) > 1)
                                        '시도 ${job.attemptCount}회',
                                      if (job.lastErrorCode != null)
                                        '오류 코드 ${job.lastErrorCode}',
                                    ].join(' · '),
                                    style: const TextStyle(
                                      fontSize: 12,
                                      color: Color(0xFF68766F),
                                    ),
                                  ),
                                ),
                              if (failed && !c.service.canRetryDeletion)
                                const Padding(
                                  padding: EdgeInsets.only(top: 4),
                                  child: Text(
                                    '서버에서 삭제를 다시 진행하는 방법은 아직 연결되지 않았어요. 상태를 다시 확인할 수 있어요.',
                                    style: TextStyle(fontSize: 12, height: 1.6),
                                  ),
                                ),
                              if (!c.complete)
                                const Text(
                                  '이미 접수된 삭제는 닫아도 취소되지 않아요.',
                                  style: TextStyle(fontSize: 12, height: 1.6),
                                ),
                            ],
                            if (c.error != null)
                              Padding(
                                padding: const EdgeInsets.only(top: 12),
                                child: Text(
                                  c.error!,
                                  style: const TextStyle(
                                    color: Color(0xFFB3261E),
                                    height: 1.6,
                                  ),
                                ),
                              ),
                            const SizedBox(height: 20),
                            if (c.busy)
                              const Padding(
                                padding: EdgeInsets.only(bottom: 16),
                                child: LinearProgressIndicator(),
                              ),
                          ],
                        ),
                      ),
                    ),
                    const SizedBox(height: 16),
                    Row(
                      children: [
                        Expanded(
                          child: OutlinedButton(
                            onPressed: c.busy
                                ? null
                                : () => Navigator.of(context).pop(c.complete),
                            child: Text(job == null ? '취소' : '닫기'),
                          ),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: FilledButton(
                            key: const ValueKey('delete-confirm'),
                            style: FilledButton.styleFrom(
                              backgroundColor: const Color(0xFFB3261E),
                            ),
                            onPressed:
                                c.busy ||
                                    !supported ||
                                    (needsConfirmation && !_understood)
                                ? null
                                : () async {
                                    if (c.complete) {
                                      Navigator.of(context).pop(true);
                                      return;
                                    }
                                    await c.execute(retry: failed);
                                  },
                            child: Text(
                              c.busy
                                  ? '확인 중…'
                                  : c.complete
                                  ? '완료'
                                  : job == null
                                  ? '삭제'
                                  : failed && c.service.canRetryDeletion
                                  ? '다시 시도'
                                  : '상태 다시 확인',
                            ),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  /// Per-step progress: what is already gone and what is still left.
  Widget _steps(DeletionJob job, bool failed) => Container(
    key: const ValueKey('deletion-steps'),
    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
    decoration: BoxDecoration(
      border: Border.all(color: replicaBorder),
      borderRadius: BorderRadius.circular(14),
    ),
    child: Column(
      children: [
        for (final step in job.steps)
          Builder(
            builder: (context) {
              final left = job.remaining.contains(step);
              final icon = !left
                  ? Icons.check_circle
                  : failed
                  ? Icons.error_outline
                  : Icons.schedule;
              final color = !left
                  ? replicaGreen
                  : failed
                  ? const Color(0xFFB3261E)
                  : const Color(0xFF68766F);
              return Padding(
                padding: const EdgeInsets.symmetric(vertical: 6),
                child: Row(
                  children: [
                    Icon(icon, size: 18, color: color),
                    const SizedBox(width: 10),
                    Expanded(child: Text(DeletionJob.stepLabel(step))),
                    Text(
                      !left
                          ? '삭제됨'
                          : failed
                          ? '남음'
                          : '진행 중',
                      style: TextStyle(fontSize: 12, color: color),
                    ),
                  ],
                ),
              );
            },
          ),
      ],
    ),
  );
}
