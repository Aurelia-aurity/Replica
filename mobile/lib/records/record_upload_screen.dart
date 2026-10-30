import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'record_controller.dart';
import 'record_models.dart';
import 'record_picker.dart';

class RecordUploadScreen extends StatefulWidget {
  const RecordUploadScreen({
    super.key,
    required this.controller,
    required this.picker,
  });
  final RecordController controller;
  final RecordPicker picker;
  @override
  State<RecordUploadScreen> createState() => _RecordUploadScreenState();
}

class _RecordUploadScreenState extends State<RecordUploadScreen> {
  bool _picking = false;
  String? _pickerError;
  late RecordKind _kind;
  @override
  void initState() {
    super.initState();
    _kind = widget.controller.selected?.kind ?? RecordKind.text;
  }

  Future<void> _pick() async {
    if (_picking) return;
    setState(() {
      _picking = true;
      _pickerError = null;
    });
    try {
      final file = await (widget.picker == pickRecordFile
          ? pickRecordFile(kind: _kind)
          : widget.picker());
      if (!mounted || file == null) return;
      if (widget.controller.service.policy.validate(file) == null &&
          file.kind != _kind) {
        setState(
          () => _pickerError = _kind == RecordKind.text
              ? '텍스트 기록은 .txt 파일을 선택해 주세요.'
              : '음성 기록은 .wav, .mp3, .m4a 파일을 선택해 주세요.',
        );
        return;
      }
      widget.controller.select(file);
    } catch (_) {
      if (mounted) setState(() => _pickerError = '파일을 열지 못했어요. 다시 선택해 주세요.');
    } finally {
      if (mounted) setState(() => _picking = false);
    }
  }

  void _changeKind(RecordKind kind) {
    if (_kind == kind) return;
    widget.controller.beginSelection();
    setState(() {
      _kind = kind;
      _pickerError = null;
    });
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: widget.controller,
    builder: (context, _) {
      final c = widget.controller;
      final file = c.selected;
      final busy = c.uploading;
      final locked = c.attempt != null;
      final editable = !busy && !locked && !_picking;
      return PopScope(
        canPop: !busy,
        child: Scaffold(
          backgroundColor: replicaBackground,
          appBar: AppBar(
            backgroundColor: replicaBackground,
            surfaceTintColor: Colors.transparent,
            titleSpacing: 0,
            title: const Text(
              '기록 올리기',
              style: TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
            ),
            automaticallyImplyLeading: false,
            leading: busy
                ? null
                : BackButton(onPressed: () => Navigator.of(context).pop()),
          ),
          body: SafeArea(
            bottom: false,
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 640),
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(20, 20, 20, 24),
                  children: [
                    const Text(
                      '어떤 기록인가요?',
                      style: TextStyle(
                        fontWeight: FontWeight.w600,
                        color: Color(0xFF4C5C54),
                      ),
                    ),
                    const SizedBox(height: 14),
                    IntrinsicHeight(
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          Expanded(child: _kindCard(RecordKind.text, editable)),
                          const SizedBox(width: 12),
                          Expanded(
                            child: _kindCard(RecordKind.audio, editable),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 22),
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 14,
                      ),
                      decoration: _card(),
                      child: Row(
                        children: [
                          Container(
                            padding: const EdgeInsets.all(12),
                            decoration: BoxDecoration(
                              color: const Color(0xFFE2EEE9),
                              borderRadius: BorderRadius.circular(14),
                            ),
                            child: Icon(
                              _kind == RecordKind.text
                                  ? Icons.insert_drive_file_outlined
                                  : Icons.graphic_eq,
                              color: replicaGreen,
                            ),
                          ),
                          const SizedBox(width: 12),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  file?.name ?? '파일을 선택해 주세요',
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    fontWeight: FontWeight.w600,
                                    fontSize: 15,
                                  ),
                                ),
                                const SizedBox(height: 5),
                                Text(
                                  file == null
                                      ? (_kind == RecordKind.text
                                            ? '.txt'
                                            : '.wav .mp3 .m4a')
                                      : '${formatBytes(file.sizeBytes)} · ${file.kind == RecordKind.text ? '텍스트 파일' : '음성 파일'}',
                                  style: _muted,
                                ),
                              ],
                            ),
                          ),
                          TextButton(
                            onPressed: editable ? _pick : null,
                            style: TextButton.styleFrom(
                              minimumSize: const Size(48, 44),
                              padding: const EdgeInsets.symmetric(
                                horizontal: 8,
                              ),
                            ),
                            child: Text(
                              _picking
                                  ? '선택 중'
                                  : file == null
                                  ? '파일 선택'
                                  : '변경',
                              style: const TextStyle(
                                decoration: TextDecoration.underline,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      '최대 ${formatBytes(c.service.policy.maxBytes)}${c.service.isPreview ? ' · 미리보기 기준' : ''}',
                      style: _muted,
                    ),
                    if (_pickerError != null || c.selectionError != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 10),
                        child: Text(
                          _pickerError ?? c.selectionError!,
                          style: const TextStyle(color: Color(0xFFB3261E)),
                        ),
                      ),
                    if (_kind == RecordKind.text) ...[
                      const SizedBox(height: 24),
                      const Text(
                        '누구의 말투를 반영할까요?',
                        style: TextStyle(
                          fontWeight: FontWeight.w600,
                          color: Color(0xFF4C5C54),
                        ),
                      ),
                      const SizedBox(height: 12),
                      Container(
                        padding: const EdgeInsets.all(16),
                        decoration: _card(),
                        child: const Text(
                          '카카오톡 대화의 화자 선택은 준비 중이에요.\n현재는 텍스트 파일 전체를 등록해요.',
                          style: TextStyle(
                            fontSize: 13,
                            height: 1.6,
                            color: Color(0xFF68766F),
                          ),
                        ),
                      ),
                      const SizedBox(height: 10),
                      const Text(
                        '공유·처리 권한이 있는 자료만 선택하고, 다른 사람의 개인정보는 올리기 전에 가려 주세요.',
                        style: _muted,
                      ),
                    ],
                    const SizedBox(height: 24),
                    Material(
                      color: const Color(0xFFE2EEE9),
                      borderRadius: BorderRadius.circular(16),
                      child: CheckboxListTile(
                        key: const ValueKey('record-consent'),
                        contentPadding: const EdgeInsets.symmetric(
                          horizontal: 10,
                          vertical: 8,
                        ),
                        controlAffinity: ListTileControlAffinity.leading,
                        activeColor: replicaGreen,
                        value: c.consent,
                        onChanged: busy || locked
                            ? null
                            : (value) => c.setConsent(value ?? false),
                        title: const Text(
                          '이 기록을 올릴 권한이 있고, 대화 상대의 말투 분석과 응답 생성에 쓰는 데 동의해요.',
                          style: TextStyle(fontSize: 14, height: 1.6),
                        ),
                      ),
                    ),
                    const SizedBox(height: 10),
                    const Text(
                      '확정된 기록만 개인화에 사용하며, 모델 학습에는 사용하지 않아요.',
                      style: _muted,
                    ),
                    if (c.service.isPreview)
                      const Padding(
                        padding: EdgeInsets.only(top: 12),
                        child: Text(
                          '미리보기 · 파일 내용은 읽거나 전송하지 않으며, 목록은 로그아웃하면 초기화돼요.',
                          style: _muted,
                        ),
                      ),
                    if (busy) ...[
                      const SizedBox(height: 20),
                      const LinearProgressIndicator(color: replicaGreen),
                      const SizedBox(height: 10),
                      Semantics(
                        liveRegion: true,
                        child: Text(switch (c.attempt?.stage) {
                          UploadStage.reserve => '업로드 준비 중…',
                          UploadStage.transfer => '파일 업로드 중…',
                          _ => '업로드 결과 확인 중…',
                        }),
                      ),
                    ],
                    if (c.uploadError != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 16),
                        child: Text(
                          c.uploadError!,
                          style: const TextStyle(color: Color(0xFFB3261E)),
                        ),
                      ),
                    if (c.uploadSucceeded)
                      const Padding(
                        padding: EdgeInsets.only(top: 16),
                        child: Text(
                          '기록이 등록됐어요.',
                          style: TextStyle(
                            color: replicaGreen,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                    if (locked && !busy && !c.uploadSucceeded)
                      TextButton(
                        onPressed: c.beginSelection,
                        child: const Text('새 파일로 시작'),
                      ),
                  ],
                ),
              ),
            ),
          ),
          bottomNavigationBar: SafeArea(
            top: false,
            child: Center(
              heightFactor: 1,
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 640),
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(20, 10, 20, 8),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      FilledButton(
                        key: const ValueKey('record-upload'),
                        style: FilledButton.styleFrom(
                          backgroundColor: replicaGreen,
                          minimumSize: const Size.fromHeight(56),
                        ),
                        onPressed: c.uploadSucceeded
                            ? () => Navigator.of(context).pop()
                            : c.canUpload
                            ? c.send
                            : null,
                        child: Text(
                          c.uploadSucceeded
                              ? '기록 목록 보기'
                              : busy
                              ? '업로드 중…'
                              : c.uploadError != null
                              ? '다시 시도'
                              : '올리기',
                          style: const TextStyle(
                            fontSize: 17,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                      if (!busy && !c.uploadSucceeded)
                        TextButton(
                          onPressed: () => Navigator.of(context).pop(),
                          child: const Text('취소하고 돌아가기'),
                        ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      );
    },
  );

  static const _muted = TextStyle(
    fontSize: 12,
    height: 1.6,
    color: Color(0xFF75817B),
  );
  BoxDecoration _card() => BoxDecoration(
    color: Colors.white,
    border: Border.all(color: replicaBorder),
    borderRadius: BorderRadius.circular(16),
  );
  Widget _kindCard(RecordKind kind, bool enabled) {
    final selected = kind == _kind;
    final audio = kind == RecordKind.audio;
    return Semantics(
      selected: selected,
      button: true,
      child: Material(
        color: selected ? const Color(0xFFF0F7F3) : Colors.white,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(16),
          side: BorderSide(
            color: selected ? replicaGreen : replicaBorder,
            width: selected ? 2 : 1,
          ),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: enabled ? () => _changeKind(kind) : null,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(
                  audio ? Icons.graphic_eq : Icons.description_outlined,
                  color: audio ? aiAmber : replicaGreen,
                ),
                const SizedBox(height: 12),
                Text(
                  audio ? '음성' : '텍스트',
                  style: const TextStyle(
                    fontWeight: FontWeight.w700,
                    fontSize: 18,
                  ),
                ),
                const SizedBox(height: 10),
                Text(
                  audio ? '.wav .mp3 .m4a' : '카카오톡 내보내기, 일기 · .txt',
                  style: _muted,
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
