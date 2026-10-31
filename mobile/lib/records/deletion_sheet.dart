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
  builder: (_) => DeletionSheet(controller: controller, preview: preview),
);

class DeletionSheet extends StatelessWidget {
  const DeletionSheet({
    super.key,
    required this.controller,
    this.preview = false,
  });
  final RecordDeletionController controller;
  final bool preview;
  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: controller,
    builder: (context, _) {
      final c = controller;
      final job = c.job;
      final failed =
          job?.status == 'failed' ||
          (job?.status == 'completed' && !job!.complete);
      final supported = job != null || !c.account || c.service.canDeleteAccount;
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
                                            style: const TextStyle(height: 1.5),
                                          ),
                                        ),
                                      ],
                                    ),
                                  ),
                              ],
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
                            Text(
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
                            if (!c.complete && job.remaining.isNotEmpty)
                              Padding(
                                padding: const EdgeInsets.only(top: 8),
                                child: Text(
                                  '남은 항목: ${job.remainingLabel}',
                                  style: const TextStyle(height: 1.6),
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
                          onPressed: c.busy || !supported
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
