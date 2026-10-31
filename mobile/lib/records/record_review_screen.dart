import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'record_controller.dart';
import 'record_models.dart';
import 'record_review.dart';
import 'deletion_sheet.dart';

class RecordReviewScreen extends StatefulWidget {
  const RecordReviewScreen({
    super.key,
    required this.records,
    required this.entry,
  });
  final RecordController records;
  final RecordEntry entry;
  @override
  State<RecordReviewScreen> createState() => _RecordReviewScreenState();
}

class _RecordReviewScreenState extends State<RecordReviewScreen> {
  late final RecordReviewController c;
  final text = TextEditingController();
  bool retrying = false;
  @override
  void initState() {
    super.initState();
    c = RecordReviewController(
      service: widget.records.service,
      recordId: widget.entry.id,
      drafts: widget.records.reviewDrafts,
    );
    c.addListener(_changed);
    _load();
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  Future<void> _load() async {
    await c.load();
    if (mounted) text.text = c.content;
  }

  @override
  void dispose() {
    c.removeListener(_changed);
    c.dispose();
    text.dispose();
    super.dispose();
  }

  Future<void> _later() async {
    if (c.busy) return;
    if (c.content != c.value?.version?.content && c.content.trim().isNotEmpty) {
      if (!await c.save() || !mounted) return;
    }
    if (mounted) Navigator.of(context).pop();
  }

  Future<void> _confirm() async {
    if (!c.canConfirm) return;
    if (!await c.save(confirm: true) || !mounted) return;
    await widget.records.refresh();
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('전사문을 확정했어요. 개인화 반영 상태를 확인해 주세요.')),
    );
    Navigator.of(context).pop();
  }

  Future<void> _retry() async {
    if (retrying) return;
    setState(() => retrying = true);
    await widget.records.retry(c.value!.record);
    if (mounted) {
      await _load();
      if (mounted) setState(() => retrying = false);
    }
  }

  Future<void> _delete() async {
    final deletion = widget.records.deletionFor(widget.entry.id);
    deletion.label = widget.entry.name;
    final complete = await showDeletionSheet(
      context,
      deletion,
      preview: widget.records.service.isPreview,
    );
    if (!mounted) return;
    if (deletion.job != null) {
      await widget.records.refresh();
      if (!mounted) return;
      if (complete == true) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('기록을 삭제했어요')));
      }
      Navigator.of(context).pop();
    }
  }

  @override
  Widget build(BuildContext context) {
    final review = c.value;
    final entry = review?.record ?? widget.entry;
    final detail = c.confirmed || entry.confirmed || entry.textConfirmed;
    final waiting = [
      RecordStatus.uploading,
      RecordStatus.uploaded,
      RecordStatus.processing,
    ].contains(entry.status);
    final failed = entry.status == RecordStatus.failed;
    return PopScope(
      canPop: !c.busy,
      child: Scaffold(
        backgroundColor: replicaBackground,
        appBar: AppBar(
          actions: [
            IconButton(
              tooltip: '기록 삭제',
              onPressed: c.busy ? null : _delete,
              icon: const Icon(Icons.delete_outline),
            ),
          ],
          backgroundColor: replicaBackground,
          surfaceTintColor: Colors.transparent,
          automaticallyImplyLeading: false,
          leading: c.busy
              ? null
              : BackButton(onPressed: () => Navigator.of(context).pop()),
          titleSpacing: 0,
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                detail ? '기록 상세' : '전사 확인',
                style: const TextStyle(
                  fontSize: 20,
                  fontWeight: FontWeight.w700,
                ),
              ),
              Text(
                entry.name,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 12, color: Color(0xFF75817B)),
              ),
            ],
          ),
        ),
        body: SafeArea(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 640),
              child: ListView(
                padding: const EdgeInsets.all(20),
                children: [
                  if (widget.records.service.isPreview)
                    const Padding(
                      padding: EdgeInsets.only(bottom: 12),
                      child: Text(
                        '미리보기 · 아래 전사문과 처리 결과는 합성 예시예요.',
                        style: TextStyle(
                          fontSize: 12,
                          color: Color(0xFF75817B),
                        ),
                      ),
                    ),
                  if (entry.kind == RecordKind.audio)
                    Container(
                      padding: const EdgeInsets.all(16),
                      decoration: BoxDecoration(
                        color: const Color(0xFF19241F),
                        borderRadius: BorderRadius.circular(18),
                      ),
                      child: const Row(
                        children: [
                          Icon(
                            Icons.play_circle_fill,
                            size: 44,
                            color: Colors.white54,
                          ),
                          SizedBox(width: 12),
                          Expanded(
                            child: Text(
                              '원본 음성 재생은 연결 준비 중이에요.\n전사 구간 시간은 아직 제공되지 않아요.',
                              style: TextStyle(
                                color: Colors.white70,
                                fontSize: 12,
                                height: 1.6,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  const SizedBox(height: 16),
                  Container(
                    padding: const EdgeInsets.all(16),
                    decoration: BoxDecoration(
                      color: const Color(0xFFE2EEE9),
                      borderRadius: BorderRadius.circular(16),
                    ),
                    child: Text(
                      detail
                          ? '전사문이 확정됐어요. 개인화 반영 여부는 아래 상태를 확인해 주세요.'
                          : '잘못 들린 부분을 고쳐 주세요. 확정한 전사문만 말투 프로필과 대화에 쓰여요.',
                      style: const TextStyle(fontSize: 14, height: 1.6),
                    ),
                  ),
                  const SizedBox(height: 16),
                  if (c.loading) const LinearProgressIndicator(),
                  if (c.error != null) ...[
                    Text(
                      c.error!,
                      style: const TextStyle(color: Color(0xFFB3261E)),
                    ),
                    TextButton(
                      onPressed: c.busy ? null : _load,
                      child: const Text('최신 내용 다시 확인'),
                    ),
                  ],
                  if (!c.loading && review != null) ...[
                    if (failed) ...[
                      if (widget.records.actionError != null)
                        Text(
                          widget.records.actionError!,
                          style: const TextStyle(color: Color(0xFFB3261E)),
                        ),
                      const Text('전사에 실패했어요'),
                      OutlinedButton.icon(
                        onPressed:
                            widget.records.service.canRetryProcessing &&
                                !retrying
                            ? _retry
                            : null,
                        icon: const Icon(Icons.refresh),
                        label: Text(retrying ? '다시 요청 중…' : '다시 시도'),
                      ),
                      if (!widget.records.service.canRetryProcessing)
                        const Text('전사 재시도는 아직 연결되지 않았어요.'),
                    ] else if (waiting) ...[
                      const LinearProgressIndicator(),
                      const SizedBox(height: 12),
                      const Text('전사 중이에요. 완료되면 내용을 확인할 수 있어요.'),
                      TextButton(
                        onPressed: _load,
                        child: const Text('상태 새로고침'),
                      ),
                    ] else if (review.version == null) ...[
                      const Text('아직 전사문이 없어요. 잠시 후 다시 확인해 주세요.'),
                      TextButton(onPressed: _load, child: const Text('다시 확인')),
                    ] else if (detail) ...[
                      _detail(entry, review),
                      const SizedBox(height: 20),
                      OutlinedButton.icon(
                        onPressed: c.busy ? null : _delete,
                        style: OutlinedButton.styleFrom(
                          foregroundColor: const Color(0xFFB3261E),
                          side: const BorderSide(color: Color(0xFFE6B6B2)),
                        ),
                        icon: const Icon(Icons.delete_outline),
                        label: const Text('이 기록 삭제'),
                      ),
                    ] else
                      TextField(
                        key: const ValueKey('transcript-editor'),
                        controller: text,
                        onChanged: c.edit,
                        enabled: !c.busy && !c.conflict,
                        minLines: 8,
                        maxLines: null,
                        decoration: InputDecoration(
                          labelText: '전사문 수정',
                          alignLabelWithHint: true,
                          filled: true,
                          fillColor: Colors.white,
                          border: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(16),
                          ),
                          focusedBorder: OutlineInputBorder(
                            borderSide: const BorderSide(
                              color: replicaGreen,
                              width: 2,
                            ),
                            borderRadius: BorderRadius.circular(16),
                          ),
                        ),
                      ),
                    if (!detail &&
                        widget.records.reviewDrafts.containsKey(entry.id))
                      const Padding(
                        padding: EdgeInsets.only(top: 12),
                        child: Text(
                          '수정 내용은 현재 로그인 동안 임시 보관돼요.',
                          style: TextStyle(fontSize: 12, color: replicaGreen),
                        ),
                      ),
                    if (!detail &&
                        review.version != null &&
                        c.content != review.version!.content)
                      ExpansionTile(
                        title: const Text(
                          '서버에 저장된 내용과 비교',
                          style: TextStyle(fontSize: 13),
                        ),
                        children: [
                          Padding(
                            padding: const EdgeInsets.all(16),
                            child: Text(
                              review.version!.content,
                              style: const TextStyle(height: 1.6),
                            ),
                          ),
                        ],
                      ),
                  ],
                ],
              ),
            ),
          ),
        ),
        bottomNavigationBar: SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.all(20),
            child: Row(
              children: [
                Expanded(
                  child: OutlinedButton(
                    onPressed: c.busy ? null : _later,
                    child: Text(detail ? '목록으로' : '나중에'),
                  ),
                ),
                const SizedBox(width: 12),
                if (!detail)
                  Expanded(
                    flex: 2,
                    child: FilledButton(
                      key: const ValueKey('transcript-confirm'),
                      onPressed: c.canConfirm ? _confirm : null,
                      style: FilledButton.styleFrom(
                        backgroundColor: replicaGreen,
                      ),
                      child: Text(c.busy ? '저장 중…' : '확정하기'),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _detail(RecordEntry entry, RecordReview review) {
    final date = entry.createdAt.toLocal();
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: Colors.white,
        border: Border.all(color: replicaBorder),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            entry.name,
            style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 20),
          Text('종류    ${entry.kind == RecordKind.audio ? '음성' : '텍스트'}'),
          const SizedBox(height: 12),
          Text(
            '올린 날    ${date.year}.${date.month.toString().padLeft(2, '0')}.${date.day.toString().padLeft(2, '0')}',
          ),
          const SizedBox(height: 12),
          Text('상태    ${c.confirmed ? '전사문 확정' : entry.statusLabel}'),
          if (review.version?.confirmedAt != null)
            Text(
              '확정일    ${review.version!.confirmedAt!.toLocal().toString().split(' ').first}',
            ),
          const Divider(height: 32),
          Text(
            review.version?.content ?? '',
            style: const TextStyle(height: 1.7),
          ),
          const Divider(height: 32),
          Text(switch (review.profile) {
            ReviewProfileState.reflected => '반영됨 · 미리보기 합성 결과',
            ReviewProfileState.processing => '개인화 반영 중',
            ReviewProfileState.failed => '개인화 반영에 실패했어요.',
            ReviewProfileState.available =>
              '사용 가능한 말투 프로필이 있어요. 이 기록의 반영 여부는 확인이 필요해요.',
            ReviewProfileState.unknown => '개인화 반영 상태를 아직 확인할 수 없어요.',
          }),
          TextButton(
            onPressed: c.loading ? null : _load,
            child: const Text('반영 상태 새로고침'),
          ),
        ],
      ),
    );
  }
}
