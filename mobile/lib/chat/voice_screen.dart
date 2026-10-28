import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';

/// SC-10 presentation only. Recording, STT and TTS need backend contracts.
class VoiceScreen extends StatelessWidget {
  const VoiceScreen({
    super.key,
    required this.conversationName,
    required this.onKeyboard,
    this.lastQuestion,
    this.lastReply,
  });

  final String conversationName;
  final VoidCallback onKeyboard;
  final String? lastQuestion;
  final String? lastReply;
  static const _surface = Color(0xFF17231D);
  static const _card = Color(0xFF24352C);

  void _notice(BuildContext context) {
    ScaffoldMessenger.of(context).hideCurrentSnackBar();
    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
      content: Text('음성 녹음·인식·재생은 아직 연결되지 않았어요. 키보드로 대화를 이어가 주세요.'),
    ));
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: _surface,
    appBar: AppBar(
      backgroundColor: _surface,
      foregroundColor: Colors.white,
      surfaceTintColor: Colors.transparent,
      title: Text(conversationName),
      actions: const [Padding(padding: EdgeInsets.only(right: 16), child: Center(child: AiBadge(label: 'AI 생성 대화')))],
    ),
    body: SafeArea(top: false, child: Center(child: ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: 720),
      child: Column(children: [
        const Padding(
          padding: EdgeInsets.fromLTRB(16, 8, 16, 16),
          child: Row(children: [
            _VoiceStage(label: '녹음'),
            _VoiceStage(label: '인식'),
            _VoiceStage(label: '응답 생성'),
            _VoiceStage(label: '음성 합성'),
          ]),
        ),
        Expanded(child: ListView(padding: const EdgeInsets.symmetric(horizontal: 20), children: [
          const Text('음성 화면 미리보기 · 마이크는 사용하지 않습니다.',
            style: TextStyle(color: Color(0xFFB2C8BB), fontSize: 12), textAlign: TextAlign.center),
          const SizedBox(height: 24),
          const Text('내 질문', textAlign: TextAlign.right,
            style: TextStyle(color: Color(0xFFB2C8BB), fontSize: 12)),
          const SizedBox(height: 8),
          Align(alignment: Alignment.centerRight, child: Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(color: replicaGreen, borderRadius: BorderRadius.circular(20)),
            child: Text(lastQuestion ?? '아직 질문이 없어요. 키보드로 먼저 이야기를 시작해 보세요.',
              style: const TextStyle(color: Colors.white, height: 1.6)),
          )),
          const SizedBox(height: 20),
          Container(
            padding: const EdgeInsets.all(16),
            decoration: BoxDecoration(color: aiAmberBackground, borderRadius: BorderRadius.circular(16)),
            child: const Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Icon(Icons.info_outline, color: aiAmber, size: 18),
              SizedBox(width: 8),
              Expanded(child: Text('음성 응답은 기록을 바탕으로 AI가 합성한 음성이에요. 현재는 재생 기능을 제공하지 않습니다.',
                style: TextStyle(color: aiAmber, height: 1.5, fontSize: 13))),
            ]),
          ),
          const SizedBox(height: 16),
          Container(
            padding: const EdgeInsets.all(18),
            decoration: BoxDecoration(color: _card, borderRadius: BorderRadius.circular(20)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                IconButton(
                  tooltip: '음성 재생 준비 중', onPressed: () => _notice(context),
                  icon: const Icon(Icons.play_arrow, color: Colors.white),
                ),
                const SizedBox(width: 8),
                const Expanded(child: Text('음성 재생 준비 중', style: TextStyle(color: Color(0xFFB2C8BB)))),
              ]),
              const SizedBox(height: 12),
              Text(lastReply ?? '답변이 도착하면 이곳에 표시돼요.',
                style: const TextStyle(color: Colors.white, height: 1.6)),
              if (lastReply != null) const Padding(padding: EdgeInsets.only(top: 16), child: AiBadge()),
            ]),
          ),
          const SizedBox(height: 20),
        ])),
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 24),
          child: Row(crossAxisAlignment: CrossAxisAlignment.center, children: [
            IconButton.filledTonal(
              tooltip: '텍스트 대화로 전환', onPressed: onKeyboard,
              style: IconButton.styleFrom(backgroundColor: _card, foregroundColor: Colors.white),
              icon: const Icon(Icons.keyboard_outlined),
            ),
            Expanded(child: Column(mainAxisSize: MainAxisSize.min, children: [
              SizedBox(width: 80, height: 80, child: IconButton.filled(
                tooltip: '음성 기능 준비 중', onPressed: () => _notice(context),
                style: IconButton.styleFrom(backgroundColor: const Color(0xFF84C5AD), foregroundColor: _surface),
                icon: const Icon(Icons.mic_none, size: 36),
              )),
              const SizedBox(height: 10),
              const Text('음성 기능 준비 중', style: TextStyle(color: Color(0xFFB2C8BB), fontSize: 13)),
            ])),
            const SizedBox(width: 48),
          ]),
        ),
      ]),
    ))),
  );
}

class _VoiceStage extends StatelessWidget {
  const _VoiceStage({required this.label});
  final String label;

  @override
  Widget build(BuildContext context) => Expanded(child: Column(children: [
    const Icon(Icons.circle_outlined, size: 12, color: Color(0xFF7B9385)),
    const SizedBox(height: 8),
    Text(label, textAlign: TextAlign.center, style: const TextStyle(color: Color(0xFFB2C8BB), fontSize: 11)),
  ]));
}
