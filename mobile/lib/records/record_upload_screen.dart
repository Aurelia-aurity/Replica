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
  Future<void> _pick() async {
    if (_picking) return;
    setState(() {
      _picking = true;
      _pickerError = null;
    });
    try {
      final file = await widget.picker();
      if (mounted && file != null) widget.controller.select(file);
    } catch (_) {
      if (mounted) setState(() => _pickerError = '파일을 열지 못했어요. 다시 선택해 주세요.');
    } finally {
      if (mounted) setState(() => _picking = false);
    }
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: widget.controller,
    builder: (context, _) {
      final controller = widget.controller;
      final file = controller.selected;
      final busy = controller.uploading;
      final locked = controller.attempt != null;
      return PopScope(
        canPop: !busy,
        child: Scaffold(
          appBar: AppBar(
            title: const Text('기록 올리기'),
            automaticallyImplyLeading: !busy,
          ),
          body: SafeArea(
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 640),
                child: ListView(
                  padding: const EdgeInsets.all(24),
                  children: [
                    const Text(
                      '남겨둔 기록을 올려 주세요',
                      style: TextStyle(
                        fontSize: 24,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 12),
                    const Text(
                      '텍스트 대화·일기 또는 음성 파일을 선택할 수 있어요.',
                      style: TextStyle(height: 1.6),
                    ),
                    const SizedBox(height: 20),
                    Container(
                      padding: const EdgeInsets.all(20),
                      decoration: BoxDecoration(
                        color: Colors.white,
                        borderRadius: BorderRadius.circular(18),
                        border: Border.all(color: replicaBorder),
                      ),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          const Icon(
                            Icons.upload_file_outlined,
                            size: 40,
                            color: replicaGreen,
                          ),
                          const SizedBox(height: 16),
                          if (file != null) ...[
                            Text(
                              file.name,
                              style: const TextStyle(
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                            const SizedBox(height: 8),
                            Text(
                              '${file.extension.toUpperCase()} · ${formatBytes(file.sizeBytes)}',
                            ),
                            const SizedBox(height: 16),
                          ],
                          OutlinedButton.icon(
                            onPressed: busy || locked || _picking
                                ? null
                                : _pick,
                            icon: const Icon(Icons.folder_open),
                            label: Text(
                              _picking
                                  ? '파일 선택 중'
                                  : file == null
                                  ? '파일 선택'
                                  : '다른 파일 선택',
                            ),
                          ),
                          const SizedBox(height: 12),
                          Text(
                            '.txt, .wav, .mp3, .m4a · 최대 ${formatBytes(controller.service.policy.maxBytes)}${controller.service.isPreview ? ' (미리보기 기준)' : ''}',
                            style: const TextStyle(
                              fontSize: 12,
                              color: Color(0xFF68766F),
                            ),
                          ),
                        ],
                      ),
                    ),
                    if (_pickerError != null ||
                        controller.selectionError != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 12),
                        child: Text(
                          _pickerError ?? controller.selectionError!,
                          style: const TextStyle(color: Color(0xFFB3261E)),
                        ),
                      ),
                    const SizedBox(height: 24),
                    const Text(
                      '자료 사용 동의·권한 확인',
                      style: TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 12),
                    const Text(
                      '본인이 소유하거나 공유·처리 권한을 확인한 자료만 올려 주세요. 기록은 선택한 대화 상대의 말투 분석과 개인화에 사용돼요. 전사문을 확인하고 확정한 뒤 대화에 반영돼요. 모델 학습에는 동의하지 않습니다.',
                      style: TextStyle(height: 1.7),
                    ),
                    CheckboxListTile(
                      key: const ValueKey('record-consent'),
                      contentPadding: EdgeInsets.zero,
                      value: controller.consent,
                      onChanged: busy || locked
                          ? null
                          : (value) => controller.setConsent(value ?? false),
                      controlAffinity: ListTileControlAffinity.leading,
                      title: const Text(
                        '자료 사용 권한이 있으며 개인화 목적의 처리에 동의해요.',
                        style: TextStyle(fontSize: 14),
                      ),
                    ),
                    if (controller.service.isPreview)
                      const Padding(
                        padding: EdgeInsets.symmetric(vertical: 12),
                        child: Text(
                          '미리보기 모드예요. 선택한 파일의 내용은 읽거나 전송하지 않으며, 파일 정보만 현재 로그인 동안 목록에 표시해요.',
                          style: TextStyle(
                            fontSize: 12,
                            height: 1.6,
                            color: Color(0xFF68766F),
                          ),
                        ),
                      ),
                    if (busy) ...[
                      const LinearProgressIndicator(),
                      const SizedBox(height: 12),
                      Semantics(
                        liveRegion: true,
                        child: Text(switch (controller.attempt?.stage) {
                          UploadStage.reserve => '업로드 준비 중…',
                          UploadStage.transfer => '파일 업로드 중…',
                          _ => '업로드 결과 확인 중…',
                        }),
                      ),
                    ],
                    if (controller.uploadError != null)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 12),
                        child: Text(
                          controller.uploadError!,
                          style: const TextStyle(color: Color(0xFFB3261E)),
                        ),
                      ),
                    if (controller.uploadSucceeded) ...[
                      const Text(
                        '기록이 등록됐어요.',
                        style: TextStyle(
                          color: replicaGreen,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 16),
                      FilledButton(
                        onPressed: () => Navigator.of(context).pop(),
                        child: const Text('기록 목록 보기'),
                      ),
                    ] else ...[
                      const SizedBox(height: 16),
                      FilledButton(
                        key: const ValueKey('record-upload'),
                        onPressed: controller.canUpload
                            ? controller.send
                            : null,
                        child: Text(
                          busy
                              ? '업로드 중…'
                              : controller.uploadError != null
                              ? '다시 시도'
                              : '동의하고 올리기',
                        ),
                      ),
                      if (locked && !busy)
                        TextButton(
                          onPressed: controller.beginSelection,
                          child: const Text('새 파일로 시작'),
                        ),
                    ],
                  ],
                ),
              ),
            ),
          ),
        ),
      );
    },
  );
}
