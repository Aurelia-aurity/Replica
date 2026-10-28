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
                  if (controller.service.isPreview) ...[
                    const Text(
                      '미리보기 · 파일은 서버로 전송되지 않으며 로그아웃하면 목록이 초기화돼요.',
                      style: TextStyle(
                        fontSize: 12,
                        color: Color(0xFF68766F),
                        height: 1.5,
                      ),
                    ),
                    const SizedBox(height: 12),
                  ],
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
    final color = failed
        ? const Color(0xFFB3261E)
        : audio
        ? aiAmber
        : replicaGreen;
    final background = failed
        ? const Color(0xFFFCE8E6)
        : audio
        ? aiAmberBackground
        : const Color(0xFFE3EFEB);
    final date = entry.createdAt.toLocal();
    final seconds = entry.durationSeconds;
    final metadata = [
      audio ? '음성' : '텍스트',
      if (seconds != null) '${seconds ~/ 60}분 ${seconds % 60}초',
      if (entry.sizeBytes != null) formatBytes(entry.sizeBytes!),
      '${date.month}월 ${date.day}일',
    ].join(' · ');
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
        padding: const EdgeInsets.all(16),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: background,
                borderRadius: BorderRadius.circular(14),
              ),
              child: Icon(
                failed
                    ? Icons.warning_amber_rounded
                    : audio
                    ? Icons.graphic_eq
                    : Icons.description_outlined,
                color: color,
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
                      fontSize: 16,
                    ),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    failed ? '기록 처리에 실패했어요' : metadata,
                    style: TextStyle(
                      color: failed ? color : const Color(0xFF68766F),
                      fontSize: 12,
                      height: 1.5,
                    ),
                  ),
                  const SizedBox(height: 8),
                  if (failed && widget.controller.service.canRetryProcessing)
                    OutlinedButton.icon(
                      onPressed: widget.controller.retrying.contains(entry.id)
                          ? null
                          : () => widget.controller.retry(entry),
                      icon: const Icon(Icons.refresh, size: 16),
                      label: const Text('다시 시도'),
                    )
                  else
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 9,
                        vertical: 4,
                      ),
                      decoration: BoxDecoration(
                        color:
                            entry.status == RecordStatus.ready &&
                                !entry.confirmed
                            ? aiAmberBackground
                            : background,
                        borderRadius: BorderRadius.circular(20),
                      ),
                      child: Text(
                        entry.statusLabel,
                        style: TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.w600,
                          color:
                              entry.status == RecordStatus.ready &&
                                  !entry.confirmed
                              ? aiAmber
                              : color,
                        ),
                      ),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
