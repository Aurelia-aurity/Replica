import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'record_controller.dart';
import 'record_models.dart';
import 'record_picker.dart';
import 'record_upload_screen.dart';

class RecordsPanel extends StatefulWidget {
  const RecordsPanel({
    super.key,
    required this.controller,
    this.picker = pickRecordFile,
  });
  final RecordController controller;
  final RecordPicker picker;
  @override
  State<RecordsPanel> createState() => _RecordsPanelState();
}

class _RecordsPanelState extends State<RecordsPanel> {
  RecordKind? _filter;
  void _upload() {
    widget.controller.beginSelection();
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => RecordUploadScreen(
          controller: widget.controller,
          picker: widget.picker,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) => SafeArea(
    child: AnimatedBuilder(
      animation: widget.controller,
      builder: (context, _) {
        final controller = widget.controller;
        final records = controller.entries
            .where((e) => _filter == null || e.kind == _filter)
            .toList();
        return Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: RefreshIndicator(
              onRefresh: controller.refresh,
              child: ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(20, 28, 20, 24),
                children: [
                  Row(
                    children: [
                      const Expanded(
                        child: Text(
                          '기록',
                          style: TextStyle(
                            fontSize: 28,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                      FilledButton.icon(
                        onPressed: _upload,
                        style: FilledButton.styleFrom(
                          backgroundColor: replicaGreen,
                          shape: const StadiumBorder(),
                          padding: const EdgeInsets.symmetric(horizontal: 16),
                        ),
                        icon: const Icon(Icons.add),
                        label: const Text('올리기'),
                      ),
                    ],
                  ),
                  const SizedBox(height: 20),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      for (final kind in [null, ...RecordKind.values])
                        ChoiceChip(
                          label: Text(
                            '${kind == null
                                ? '전체'
                                : kind == RecordKind.text
                                ? '텍스트'
                                : '음성'} ${controller.entries.where((e) => kind == null || e.kind == kind).length}',
                          ),
                          selected: _filter == kind,
                          backgroundColor: Colors.white,
                          shape: const StadiumBorder(),
                          side: BorderSide(
                            color: _filter == kind
                                ? const Color(0xFF19241F)
                                : replicaBorder,
                          ),
                          showCheckmark: false,
                          selectedColor: const Color(0xFF19241F),
                          labelStyle: TextStyle(
                            color: _filter == kind
                                ? Colors.white
                                : const Color(0xFF44554D),
                          ),
                          onSelected: (_) => setState(() => _filter = kind),
                        ),
                    ],
                  ),
                  const SizedBox(height: 20),
                  if (controller.loading) const LinearProgressIndicator(),
                  if (controller.listError != null)
                    _message(
                      controller.listError!,
                      '다시 시도',
                      controller.refresh,
                    ),
                  if (!controller.loading &&
                      controller.listError == null &&
                      controller.entries.isEmpty)
                    _message('아직 올린 기록이 없어요.', '첫 기록 올리기', _upload),
                  if (controller.entries.isNotEmpty && records.isEmpty)
                    const Padding(
                      padding: EdgeInsets.all(24),
                      child: Text('이 유형의 기록이 없어요.'),
                    ),
                  if (records.isNotEmpty)
                    Container(
                      clipBehavior: Clip.antiAlias,
                      decoration: BoxDecoration(
                        color: Colors.white,
                        borderRadius: BorderRadius.circular(18),
                        border: Border.all(color: replicaBorder),
                      ),
                      child: Column(
                        children: [
                          for (var i = 0; i < records.length; i++) ...[
                            if (i > 0)
                              const Divider(height: 1, color: replicaBorder),
                            _record(records[i]),
                          ],
                        ],
                      ),
                    ),
                  if (controller.actionError != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 12),
                      child: Text(
                        controller.actionError!,
                        style: const TextStyle(color: Colors.red),
                      ),
                    ),
                  const SizedBox(height: 20),
                  const Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(
                        Icons.info_outline,
                        size: 18,
                        color: Color(0xFF68766F),
                      ),
                      SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          '확정된 기록만 말투 프로필과 대화에 쓰여요.',
                          style: TextStyle(
                            color: Color(0xFF526159),
                            height: 1.5,
                          ),
                        ),
                      ),
                    ],
                  ),
                  if (controller.service.isPreview)
                    const Padding(
                      padding: EdgeInsets.only(top: 12),
                      child: Text(
                        '미리보기 · 파일은 서버로 전송되지 않으며 로그아웃하면 목록이 초기화돼요.',
                        style: TextStyle(
                          fontSize: 11,
                          height: 1.5,
                          color: Color(0xFF75817B),
                        ),
                      ),
                    ),
                  if (!controller.service.canRetryProcessing &&
                      records.any((e) => e.status == RecordStatus.failed))
                    const Padding(
                      padding: EdgeInsets.only(top: 12),
                      child: Text(
                        '전사 재시도는 백엔드 API 합의 후 제공됩니다.',
                        style: TextStyle(fontSize: 12),
                      ),
                    ),
                  Align(
                    alignment: Alignment.centerRight,
                    child: TextButton.icon(
                      onPressed: controller.loading ? null : controller.refresh,
                      icon: const Icon(Icons.refresh, size: 18),
                      label: const Text('목록 새로고침'),
                    ),
                  ),
                ],
              ),
            ),
          ),
        );
      },
    ),
  );

  Widget _message(String text, String label, VoidCallback action) => Container(
    padding: const EdgeInsets.all(24),
    margin: const EdgeInsets.only(top: 12),
    decoration: BoxDecoration(
      color: Colors.white,
      borderRadius: BorderRadius.circular(18),
    ),
    child: Column(
      children: [
        Text(text, textAlign: TextAlign.center),
        const SizedBox(height: 16),
        OutlinedButton(onPressed: action, child: Text(label)),
      ],
    ),
  );

  Widget _record(RecordEntry entry) {
    final failed = entry.status == RecordStatus.failed;
    final audio = entry.kind == RecordKind.audio;
    final date = entry.createdAt.toLocal();
    final seconds = entry.durationSeconds;
    final metadata = [
      audio ? '음성' : '텍스트',
      if (seconds != null) '${seconds ~/ 60}분 ${seconds % 60}초',
      '${date.month}월 ${date.day}일',
    ].join(' · ');
    final iconColor = failed
        ? const Color(0xFFBF3328)
        : audio
        ? aiAmber
        : replicaGreen;
    final iconBackground = failed
        ? const Color(0xFFFCE5E2)
        : audio
        ? aiAmberBackground
        : const Color(0xFFE2EEE9);
    final needsReview = entry.status == RecordStatus.ready && !entry.confirmed;
    final reflected = entry.status == RecordStatus.ready && entry.confirmed;
    final badgeColor = failed
        ? const Color(0xFFBF3328)
        : needsReview
        ? aiAmber
        : reflected
        ? replicaGreen
        : const Color(0xFF56645D);
    final badgeBackground = failed
        ? const Color(0xFFFCE5E2)
        : needsReview
        ? aiAmberBackground
        : reflected
        ? const Color(0xFFDCEEE5)
        : const Color(0xFFE5EAE7);
    final status = failed
        ? OutlinedButton.icon(
            onPressed:
                widget.controller.service.canRetryProcessing &&
                    !widget.controller.retrying.contains(entry.id)
                ? () => widget.controller.retry(entry)
                : null,
            style: OutlinedButton.styleFrom(
              minimumSize: const Size(0, 38),
              padding: const EdgeInsets.symmetric(horizontal: 10),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(10),
              ),
            ),
            icon: const Icon(Icons.refresh, size: 16),
            label: const Text('다시 시도', style: TextStyle(fontSize: 12)),
          )
        : Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
            decoration: BoxDecoration(
              color: badgeBackground,
              borderRadius: BorderRadius.circular(20),
            ),
            child: Text(
              entry.statusLabel,
              style: TextStyle(
                fontSize: 11,
                fontWeight: FontWeight.w600,
                color: badgeColor,
              ),
            ),
          );
    return InkWell(
      onTap: () => showModalBottomSheet<void>(
        context: context,
        builder: (_) => SafeArea(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  entry.name,
                  style: const TextStyle(fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 12),
                Text(entry.statusLabel),
                const SizedBox(height: 12),
                Text(
                  entry.status == RecordStatus.ready
                      ? '전사 확인·확정 화면은 이슈 #10에서 제공할 예정이에요.'
                      : '처리 상태는 목록 새로고침으로 확인할 수 있어요.',
                ),
              ],
            ),
          ),
        ),
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 18),
        child: LayoutBuilder(
          builder: (context, constraints) {
            final stacked =
                constraints.maxWidth < 280 ||
                MediaQuery.textScalerOf(context).scale(14) > 19;
            return Row(
              crossAxisAlignment: CrossAxisAlignment.center,
              children: [
                Container(
                  padding: const EdgeInsets.all(11),
                  decoration: BoxDecoration(
                    color: iconBackground,
                    borderRadius: BorderRadius.circular(14),
                  ),
                  child: Icon(
                    failed
                        ? Icons.warning_amber_rounded
                        : audio
                        ? Icons.graphic_eq
                        : Icons.description_outlined,
                    color: iconColor,
                    size: 22,
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        entry.name,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontWeight: FontWeight.w600,
                          fontSize: 15,
                        ),
                      ),
                      const SizedBox(height: 6),
                      Text(
                        failed
                            ? (audio ? '전사에 실패했어요' : '기록 처리에 실패했어요')
                            : metadata,
                        style: TextStyle(
                          color: failed ? iconColor : const Color(0xFF75817B),
                          fontSize: 12,
                          height: 1.5,
                        ),
                      ),
                      if (stacked) ...[const SizedBox(height: 8), status],
                    ],
                  ),
                ),
                if (!stacked) ...[const SizedBox(width: 10), status],
              ],
            );
          },
        ),
      ),
    );
  }
}
