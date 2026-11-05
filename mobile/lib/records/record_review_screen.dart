import 'dart:async';

import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'deletion_sheet.dart';
import 'record_controller.dart';
import 'record_models.dart';
import 'record_playback.dart';
import 'record_review.dart';
import 'transcript_segments.dart';

class RecordReviewScreen extends StatefulWidget {
  const RecordReviewScreen({
    super.key,
    required this.records,
    required this.entry,
    this.playbackEngineFactory,
    this.pollInterval = const Duration(seconds: 5),
  });
  final RecordController records;
  final RecordEntry entry;

  /// Test hook: replaces the audio engine of the player bar.
  final RecordPlaybackEngine Function(RecordAudioSource source)?
  playbackEngineFactory;

  /// Delay between quiet status checks while transcription or reflection runs.
  final Duration pollInterval;
  @override
  State<RecordReviewScreen> createState() => _RecordReviewScreenState();
}

class _RecordReviewScreenState extends State<RecordReviewScreen>
    with WidgetsBindingObserver {
  late final RecordReviewController c;
  RecordPlayerController? _player;
  final text = TextEditingController();
  final _segmentText = TextEditingController();
  bool retrying = false;
  bool _fullEdit = false;
  int? _editing;
  double? _drag;
  Timer? _wait;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    c = RecordReviewController(
      service: widget.records.service,
      recordId: widget.entry.id,
      drafts: widget.records.reviewDrafts,
    );
    if (widget.entry.kind == RecordKind.audio &&
        widget.records.service.canPlayOriginal) {
      _player = RecordPlayerController(
        resolve: () => widget.records.service.originalAudio(widget.entry.id),
        engineFactory: widget.playbackEngineFactory,
      );
    }
    c.addListener(_changed);
    _load();
  }

  void _changed() {
    if (!mounted) return;
    setState(() {});
    _syncWait();
  }

  bool get _waiting {
    final entry = c.value?.record ?? widget.entry;
    return entry.status == RecordStatus.uploading ||
        entry.status == RecordStatus.uploaded ||
        entry.status == RecordStatus.processing ||
        (c.confirmed && c.value?.profile == ReviewProfileState.processing);
  }

  /// Quietly re-reads the record while it is still being processed.
  void _syncWait() {
    if (!_waiting || c.loading || c.busy) {
      _wait?.cancel();
      _wait = null;
      return;
    }
    if (_wait != null) return;
    _wait = Timer(widget.pollInterval, () async {
      _wait = null;
      if (!mounted) return;
      await c.load(quiet: true);
      if (mounted) {
        if (!_fullEdit) text.text = c.content;
        _syncWait();
      }
    });
  }

  Future<void> _load() async {
    await c.load();
    if (mounted) text.text = c.content;
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) unawaited(_player?.pause());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _wait?.cancel();
    c.removeListener(_changed);
    c.dispose();
    _player?.dispose();
    text.dispose();
    _segmentText.dispose();
    super.dispose();
  }

  Future<void> _later() async {
    if (c.busy) return;
    final unresolved = c.needsResolution || c.conflict;
    if (!unresolved &&
        c.content != c.value?.version?.content &&
        c.content.trim().isNotEmpty) {
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
    await _player?.pause();
    if (!mounted) return;
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

  void _startEdit(int index, TranscriptSegment segment) {
    if (c.busy || c.confirmed || c.needsResolution) return;
    setState(() {
      _editing = index;
      _segmentText.text = segment.text;
      _segmentText.selection = TextSelection.collapsed(
        offset: segment.text.length,
      );
    });
  }

  void _seek(TranscriptSegment segment) {
    final start = segment.startMs;
    if (start == null) return;
    unawaited(_player?.seekTo(Duration(milliseconds: start), play: true));
  }

  @override
  Widget build(BuildContext context) {
    final review = c.value;
    final entry = review?.record ?? widget.entry;
    final detail = c.confirmed || entry.textConfirmed || entry.confirmed;
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
                  if (entry.kind == RecordKind.audio) _playerBar(),
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
                    Semantics(
                      liveRegion: true,
                      child: Text(
                        c.error!,
                        style: const TextStyle(color: Color(0xFFB3261E)),
                      ),
                    ),
                    TextButton(
                      onPressed: c.busy ? null : _load,
                      child: const Text('최신 내용 다시 확인'),
                    ),
                  ],
                  if (!c.loading && review != null) ...[
                    if (failed)
                      _failed(entry)
                    else if (waiting) ...[
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
                    ] else ...[
                      if (c.needsResolution) _resolution(review.version!),
                      _editor(),
                    ],
                    if (!detail &&
                        !failed &&
                        !waiting &&
                        widget.records.reviewDrafts.containsKey(entry.id))
                      const Padding(
                        padding: EdgeInsets.only(top: 12),
                        child: Text(
                          '수정 내용은 현재 로그인 동안 임시 보관돼요.',
                          style: TextStyle(fontSize: 12, color: replicaGreen),
                        ),
                      ),
                    if (!detail &&
                        !c.needsResolution &&
                        review.version != null &&
                        c.content != review.version!.content)
                      _compare(review.version!),
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

  // --- SC-05 item 1: original audio ----------------------------------------

  Widget _playerBar() {
    final player = _player;
    if (player == null) {
      return _darkCard(
        const Row(
          children: [
            Icon(Icons.play_circle_fill, size: 44, color: Colors.white54),
            SizedBox(width: 12),
            Expanded(
              child: Text(
                '원본 음성 재생은 아직 연결되지 않았어요.\n서버에서 원본 음성을 가져오는 방식이 정해지면 사용할 수 있어요.',
                style: TextStyle(
                  color: Colors.white70,
                  fontSize: 12,
                  height: 1.6,
                ),
              ),
            ),
          ],
        ),
      );
    }
    return AnimatedBuilder(
      animation: player,
      builder: (context, _) {
        final total = player.duration?.inMilliseconds ?? 0;
        final at = (_drag ?? player.position.inMilliseconds.toDouble()).clamp(
          0.0,
          total <= 0 ? 1.0 : total.toDouble(),
        );
        final loading = player.phase == PlayerPhase.loading;
        return _darkCard(
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  IconButton.filled(
                    key: const ValueKey('player-toggle'),
                    tooltip: player.playing ? '일시정지' : '재생',
                    style: IconButton.styleFrom(
                      backgroundColor: Colors.white,
                      foregroundColor: const Color(0xFF19241F),
                      minimumSize: const Size(48, 48),
                    ),
                    onPressed: loading ? null : player.toggle,
                    icon: loading
                        ? const SizedBox(
                            width: 20,
                            height: 20,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : Icon(player.playing ? Icons.pause : Icons.play_arrow),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: SliderTheme(
                      data: SliderTheme.of(context).copyWith(
                        activeTrackColor: const Color(0xFF8FD1B7),
                        inactiveTrackColor: Colors.white24,
                        thumbColor: Colors.white,
                        trackHeight: 3,
                      ),
                      child: Slider(
                        key: const ValueKey('player-slider'),
                        value: at,
                        max: total <= 0 ? 1.0 : total.toDouble(),
                        onChanged: total <= 0
                            ? null
                            : (value) => setState(() => _drag = value),
                        onChangeEnd: total <= 0
                            ? null
                            : (value) {
                                setState(() => _drag = null);
                                unawaited(
                                  player.seekTo(
                                    Duration(milliseconds: value.round()),
                                  ),
                                );
                              },
                      ),
                    ),
                  ),
                ],
              ),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 4),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(
                      formatClockMs(at.round()),
                      key: const ValueKey('player-position'),
                      style: _clockStyle,
                    ),
                    Text(
                      total <= 0 ? '--:--' : formatClockMs(total),
                      style: _clockStyle,
                    ),
                  ],
                ),
              ),
              if (player.phase == PlayerPhase.failed)
                const Padding(
                  padding: EdgeInsets.only(top: 8),
                  child: Text(
                    '원본 음성을 불러오지 못했어요. 다시 눌러 주세요.',
                    style: TextStyle(color: Color(0xFFFFB4AB), fontSize: 12),
                  ),
                ),
              if (widget.records.service.isPreview || player.simulated)
                const Padding(
                  padding: EdgeInsets.only(top: 8),
                  child: Text(
                    '미리보기 · 소리는 재생되지 않고 진행 시간만 표시돼요.',
                    style: TextStyle(color: Colors.white54, fontSize: 11),
                  ),
                ),
            ],
          ),
        );
      },
    );
  }

  static const _clockStyle = TextStyle(
    color: Colors.white70,
    fontSize: 12,
    fontFeatures: [FontFeature.tabularFigures()],
  );

  Widget _darkCard(Widget child) => Container(
    padding: const EdgeInsets.all(16),
    decoration: BoxDecoration(
      color: const Color(0xFF19241F),
      borderRadius: BorderRadius.circular(18),
    ),
    child: child,
  );

  // --- Failure -------------------------------------------------------------

  Widget _failed(RecordEntry entry) {
    final service = widget.records.service;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (widget.records.actionError != null)
          Text(
            widget.records.actionError!,
            style: const TextStyle(color: Color(0xFFB3261E)),
          ),
        const Text('전사에 실패했어요'),
        if (entry.errorCode != null)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(
              '오류 코드 ${entry.errorCode}',
              style: const TextStyle(fontSize: 12, color: Color(0xFF75817B)),
            ),
          ),
        OutlinedButton.icon(
          onPressed: service.canRetryProcessing && !retrying ? _retry : null,
          icon: const Icon(Icons.refresh),
          label: Text(retrying ? '다시 요청 중…' : '다시 시도'),
        ),
        if (!service.canRetryProcessing)
          const Text('전사 재시도는 아직 연결되지 않았어요. 기록을 삭제하고 다시 올릴 수 있어요.'),
      ],
    );
  }

  // --- SC-05 items 3 and 4: segments and editing ---------------------------

  Widget _editor() => ListenableBuilder(
    // The active segment follows the playback position.
    listenable: _player ?? c,
    builder: (context, _) => _editorBody(),
  );

  Widget _editorBody() {
    final doc = c.document;
    final active = _player == null || !doc.hasTimestamps
        ? null
        : doc.activeIndexAt(_player!.position.inMilliseconds);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (!doc.hasTimestamps && widget.entry.kind == RecordKind.audio)
          const Padding(
            padding: EdgeInsets.only(bottom: 10),
            child: Text(
              '구간 시간 정보가 제공되지 않아 구간 이동은 사용할 수 없어요.',
              style: TextStyle(fontSize: 12, color: Color(0xFF75817B)),
            ),
          ),
        if (_fullEdit)
          TextField(
            key: const ValueKey('transcript-editor'),
            controller: text,
            onChanged: c.edit,
            enabled: !c.busy && !c.conflict && !c.needsResolution,
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
                borderSide: const BorderSide(color: replicaGreen, width: 2),
                borderRadius: BorderRadius.circular(16),
              ),
            ),
          )
        else
          Container(
            key: const ValueKey('transcript-segments'),
            clipBehavior: Clip.antiAlias,
            decoration: BoxDecoration(
              color: Colors.white,
              border: Border.all(color: replicaBorder),
              borderRadius: BorderRadius.circular(16),
            ),
            child: Column(
              children: [
                for (var i = 0; i < doc.segments.length; i++) ...[
                  if (i > 0) const Divider(height: 1, color: replicaBorder),
                  _segmentRow(i, doc.segments[i], active: active == i),
                ],
              ],
            ),
          ),
        Align(
          alignment: Alignment.centerRight,
          child: TextButton(
            key: const ValueKey('toggle-full-edit'),
            onPressed: c.busy
                ? null
                : () => setState(() {
                    _editing = null;
                    _fullEdit = !_fullEdit;
                    text.text = c.content;
                  }),
            child: Text(_fullEdit ? '구간별로 보기' : '전체 텍스트로 수정'),
          ),
        ),
      ],
    );
  }

  Widget _segmentRow(int i, TranscriptSegment s, {required bool active}) {
    final editing = _editing == i;
    final canEdit = !c.busy && !c.confirmed && !c.needsResolution;
    return Container(
      key: ValueKey('segment-$i'),
      color: editing
          ? const Color(0xFFEFF6F2)
          : active
          ? const Color(0xFFF3F8F5)
          : null,
      padding: const EdgeInsets.fromLTRB(8, 4, 12, 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 56,
            child: s.timed
                ? Semantics(
                    button: true,
                    label: '${formatClockMs(s.startMs!)}부터 재생',
                    child: InkWell(
                      key: ValueKey('segment-time-$i'),
                      onTap: _player == null ? null : () => _seek(s),
                      child: Padding(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 4,
                          vertical: 14,
                        ),
                        child: Text(
                          formatClockMs(s.startMs!),
                          style: TextStyle(
                            fontFamily: 'monospace',
                            fontSize: 13,
                            fontWeight: FontWeight.w600,
                            color: _player == null
                                ? const Color(0xFF75817B)
                                : replicaGreen,
                          ),
                        ),
                      ),
                    ),
                  )
                : Padding(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 4,
                      vertical: 14,
                    ),
                    child: Text(
                      '${i + 1}',
                      style: const TextStyle(
                        fontSize: 13,
                        color: Color(0xFF9AA5A0),
                      ),
                    ),
                  ),
          ),
          Expanded(
            child: editing
                ? Padding(
                    padding: const EdgeInsets.symmetric(vertical: 8),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          '수정 중',
                          style: TextStyle(
                            fontSize: 11,
                            fontWeight: FontWeight.w600,
                            color: replicaGreen,
                          ),
                        ),
                        const SizedBox(height: 4),
                        TextField(
                          key: ValueKey('segment-editor-$i'),
                          controller: _segmentText,
                          autofocus: true,
                          minLines: 2,
                          maxLines: null,
                          textInputAction: TextInputAction.done,
                          onChanged: (value) => c.editSegment(i, value),
                          onSubmitted: (_) => setState(() => _editing = null),
                          decoration: InputDecoration(
                            isDense: true,
                            filled: true,
                            fillColor: Colors.white,
                            border: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(12),
                            ),
                            focusedBorder: OutlineInputBorder(
                              borderSide: const BorderSide(
                                color: replicaGreen,
                                width: 2,
                              ),
                              borderRadius: BorderRadius.circular(12),
                            ),
                          ),
                        ),
                        Align(
                          alignment: Alignment.centerRight,
                          child: TextButton(
                            key: ValueKey('segment-done-$i'),
                            onPressed: () => setState(() => _editing = null),
                            child: const Text('완료'),
                          ),
                        ),
                      ],
                    ),
                  )
                : InkWell(
                    key: ValueKey('segment-text-$i'),
                    onTap: canEdit ? () => _startEdit(i, s) : null,
                    child: Padding(
                      padding: const EdgeInsets.symmetric(vertical: 12),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (s.speaker != null)
                            Padding(
                              padding: const EdgeInsets.only(bottom: 3),
                              child: Text(
                                s.speaker!,
                                style: const TextStyle(
                                  fontSize: 11,
                                  fontWeight: FontWeight.w600,
                                  color: Color(0xFF56645D),
                                ),
                              ),
                            ),
                          Text.rich(
                            _highlight(s),
                            style: const TextStyle(fontSize: 15, height: 1.6),
                          ),
                        ],
                      ),
                    ),
                  ),
          ),
        ],
      ),
    );
  }

  /// Marks words the recognizer was unsure about (dialect, forms of address).
  TextSpan _highlight(TranscriptSegment s) {
    if (s.text.isEmpty) {
      return const TextSpan(
        text: '(비어 있음)',
        style: TextStyle(color: Color(0xFF9AA5A0)),
      );
    }
    final words = s.uncertain.where((w) => w.isNotEmpty).toSet();
    if (words.isEmpty) return TextSpan(text: s.text);
    final pattern = RegExp(words.map(RegExp.escape).join('|'));
    final spans = <TextSpan>[];
    var from = 0;
    for (final match in pattern.allMatches(s.text)) {
      if (match.start > from) {
        spans.add(TextSpan(text: s.text.substring(from, match.start)));
      }
      spans.add(
        TextSpan(
          text: match.group(0),
          style: const TextStyle(backgroundColor: aiAmberBackground),
        ),
      );
      from = match.end;
    }
    if (from < s.text.length) spans.add(TextSpan(text: s.text.substring(from)));
    return TextSpan(children: spans);
  }

  /// The server text changed under an unsaved edit: choose, never overwrite.
  Widget _resolution(TextVersion latest) => Container(
    key: const ValueKey('conflict-resolution'),
    margin: const EdgeInsets.only(bottom: 16),
    padding: const EdgeInsets.all(16),
    decoration: BoxDecoration(
      color: aiAmberBackground,
      borderRadius: BorderRadius.circular(16),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          '다른 곳에서 전사문이 바뀌었어요. 어느 쪽 내용을 쓸까요?',
          style: TextStyle(fontWeight: FontWeight.w600, height: 1.5),
        ),
        const SizedBox(height: 8),
        const Text(
          '선택하기 전에는 저장하거나 확정하지 않아요.',
          style: TextStyle(fontSize: 12, height: 1.5),
        ),
        Material(
          type: MaterialType.transparency,
          child: ExpansionTile(
            tilePadding: EdgeInsets.zero,
            title: const Text('서버의 최신 내용 보기', style: TextStyle(fontSize: 13)),
            children: [
              Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  latest.content,
                  style: const TextStyle(height: 1.6),
                ),
              ),
            ],
          ),
        ),
        Row(
          children: [
            Expanded(
              child: OutlinedButton(
                key: const ValueKey('resolve-server'),
                onPressed: () {
                  c.useServer();
                  text.text = c.content;
                },
                child: const Text('서버 내용 사용'),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: FilledButton(
                key: const ValueKey('resolve-mine'),
                style: FilledButton.styleFrom(backgroundColor: replicaGreen),
                onPressed: c.keepMine,
                child: const Text('내 수정 유지'),
              ),
            ),
          ],
        ),
      ],
    ),
  );

  Widget _compare(TextVersion latest) => ExpansionTile(
    title: const Text('서버에 저장된 내용과 비교', style: TextStyle(fontSize: 13)),
    children: [
      Padding(
        padding: const EdgeInsets.all(16),
        child: Text(latest.content, style: const TextStyle(height: 1.6)),
      ),
    ],
  );

  // --- SC-06: confirmed record ---------------------------------------------

  String _day(DateTime value) {
    final d = value.toLocal();
    return '${d.year}.${d.month.toString().padLeft(2, '0')}.${d.day.toString().padLeft(2, '0')}';
  }

  Widget _detail(RecordEntry entry, RecordReview review) {
    final version = review.version!;
    final doc = TranscriptDocument.fromContent(
      version.content,
      base: version.segments,
    );
    final confirmedAt = version.confirmedAt ?? entry.confirmedAt;
    final preview = version.content.replaceAll('\n', ' ');
    final profile = review.profile;
    final status = switch (profile) {
      ReviewProfileState.reflected => '반영됨',
      ReviewProfileState.processing => '반영 중',
      ReviewProfileState.delayed => '반영 지연',
      ReviewProfileState.failed => '반영 실패',
      _ => '확정됨',
    };
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
          Text('올린 날    ${_day(entry.createdAt)}'),
          const SizedBox(height: 12),
          Text(
            '상태    $status${confirmedAt == null ? '' : ' · ${confirmedAt.toLocal().month.toString().padLeft(2, '0')}.${confirmedAt.toLocal().day.toString().padLeft(2, '0')} 확정'}',
          ),
          if (confirmedAt != null) Text('확정일    ${_day(confirmedAt)}'),
          const Divider(height: 32),
          Text(
            preview.length > 120 ? '${preview.substring(0, 120)}…' : preview,
            style: const TextStyle(height: 1.7),
          ),
          if (doc.segments.length > 1 || preview.length > 120)
            Material(
              type: MaterialType.transparency,
              child: ExpansionTile(
                key: const ValueKey('detail-transcript'),
                tilePadding: EdgeInsets.zero,
                title: const Text('전사문 전체 보기', style: TextStyle(fontSize: 13)),
                children: [
                  for (var i = 0; i < doc.segments.length; i++)
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          SizedBox(
                            width: 52,
                            child: doc.segments[i].timed
                                ? InkWell(
                                    onTap: _player == null
                                        ? null
                                        : () => _seek(doc.segments[i]),
                                    child: Text(
                                      formatClockMs(doc.segments[i].startMs!),
                                      style: const TextStyle(
                                        fontFamily: 'monospace',
                                        fontSize: 13,
                                        color: replicaGreen,
                                      ),
                                    ),
                                  )
                                : Text('${i + 1}'),
                          ),
                          Expanded(
                            child: Text.rich(
                              _highlight(doc.segments[i]),
                              style: const TextStyle(height: 1.6),
                            ),
                          ),
                        ],
                      ),
                    ),
                ],
              ),
            ),
          const Divider(height: 32),
          _personalization(profile),
        ],
      ),
    );
  }

  Widget _personalization(ReviewProfileState state) {
    final (icon, color, message) = switch (state) {
      ReviewProfileState.reflected => (
        Icons.check_circle_outline,
        replicaGreen,
        widget.records.service.isPreview
            ? '반영됨 · 미리보기 합성 결과'
            : '반영됨 · 확정한 전사문이 말투 프로필에 반영된 것으로 확인했어요.',
      ),
      ReviewProfileState.processing => (
        Icons.autorenew,
        aiAmber,
        '개인화에 반영하는 중이에요. 잠시 후 자동으로 다시 확인해요.',
      ),
      ReviewProfileState.delayed => (
        Icons.schedule,
        aiAmber,
        '반영이 예상보다 오래 걸리고 있어요. 상태를 다시 확인해 주세요.',
      ),
      ReviewProfileState.failed => (
        Icons.error_outline,
        const Color(0xFFB3261E),
        '개인화 반영에 실패했어요.',
      ),
      ReviewProfileState.available => (
        Icons.info_outline,
        const Color(0xFF56645D),
        '사용 가능한 말투 프로필이 있어요. 이 기록의 반영 여부는 확인이 필요해요.',
      ),
      ReviewProfileState.unknown => (
        Icons.help_outline,
        const Color(0xFF56645D),
        '개인화 반영 상태를 아직 확인할 수 없어요.',
      ),
    };
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          key: ValueKey('personalization-${state.name}'),
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(icon, size: 20, color: color),
            const SizedBox(width: 10),
            Expanded(
              child: Semantics(
                liveRegion: true,
                child: Text(message, style: const TextStyle(height: 1.6)),
              ),
            ),
          ],
        ),
        TextButton(
          key: const ValueKey('profile-refresh'),
          onPressed: c.loading ? null : _load,
          child: const Text('반영 상태 새로고침'),
        ),
      ],
    );
  }
}
