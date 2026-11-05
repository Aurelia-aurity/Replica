import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'persona_controller.dart';

/// One line under the partner name on the SC-02 home card.
///
/// Built only from what the server answered. The record count is shown only
/// when the profile carries one, and "being built" only while this app itself
/// is regenerating, because the server does not report either otherwise.
String personaStatusLine(PersonaController controller) {
  final String line;
  if (controller.regeneration == RegenerationStatus.running) {
    line = '말투 프로필 다시 만드는 중';
  } else {
    line = switch (controller.profileStatus) {
      LoadStatus.idle || LoadStatus.loading => '말투 프로필 확인 중',
      LoadStatus.failed => '말투 프로필 상태를 확인하지 못했어요',
      LoadStatus.ready =>
        controller.profile == null
            ? '말투 프로필이 아직 없어요'
            : controller.profile!.evidenceRecordCount == null
            ? '말투 프로필 준비됨'
            : '말투 프로필 준비됨 · 기록 ${controller.profile!.evidenceRecordCount}개',
    };
  }
  return controller.service.isPreview ? '$line · 미리보기' : line;
}

/// SC-02 대화 상대 카드. Tapping the name area opens SC-07, or SC-13 while no
/// partner exists. The conversation buttons need a partner.
class PersonaCard extends StatelessWidget {
  const PersonaCard({
    super.key,
    required this.controller,
    required this.onOpenProfile,
    required this.onStartText,
    required this.onStartVoice,
    this.busy = false,
  });
  final PersonaController controller;
  final VoidCallback onOpenProfile, onStartText, onStartVoice;
  final bool busy;

  static const _label = Text(
    '대화 상대',
    style: TextStyle(color: Color(0xFFCAE0D6), fontSize: 12),
  );
  static const _muted = TextStyle(
    color: Color(0xFFDAEAE2),
    height: 1.5,
    fontSize: 13,
  );

  @override
  Widget build(BuildContext context) {
    final persona = controller.current;
    final loading =
        controller.personasStatus == LoadStatus.idle ||
        controller.personasStatus == LoadStatus.loading;
    final failed = controller.personasStatus == LoadStatus.failed;
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        color: replicaGreen,
        borderRadius: BorderRadius.circular(22),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (loading) ...[
            _label,
            const SizedBox(height: 12),
            const Row(
              children: [
                SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: Colors.white,
                    semanticsLabel: '대화 상대 불러오는 중',
                  ),
                ),
                SizedBox(width: 12),
                Text('불러오는 중', style: _muted),
              ],
            ),
          ] else if (failed) ...[
            _label,
            const SizedBox(height: 12),
            const Text('대화 상대를 불러오지 못했어요.', style: _muted),
            TextButton.icon(
              onPressed: controller.load,
              style: TextButton.styleFrom(foregroundColor: Colors.white),
              icon: const Icon(Icons.refresh, size: 18),
              label: const Text('다시 시도'),
            ),
          ] else if (persona == null) ...[
            _label,
            const SizedBox(height: 12),
            const Text(
              '아직 대화 상대가 없어요',
              style: TextStyle(
                color: Colors.white,
                fontSize: 22,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 12),
            const Text('말투를 학습시킬 대화 상대의 이름이나 호칭을 먼저 정해 주세요.', style: _muted),
            const SizedBox(height: 22),
            FilledButton.icon(
              onPressed: onOpenProfile,
              style: FilledButton.styleFrom(
                backgroundColor: Colors.white,
                foregroundColor: replicaGreen,
              ),
              icon: const Icon(Icons.add, size: 18),
              label: const Text('말투 프로필 만들기'),
            ),
          ] else ...[
            Semantics(
              button: true,
              label: '대화 상대 ${persona.name}, 말투 프로필 보기',
              excludeSemantics: true,
              child: InkWell(
                onTap: onOpenProfile,
                borderRadius: BorderRadius.circular(12),
                child: Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          _label,
                          const SizedBox(height: 12),
                          Text(
                            persona.name,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 30,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          const SizedBox(height: 12),
                          Text(personaStatusLine(controller), style: _muted),
                        ],
                      ),
                    ),
                    const Icon(Icons.chevron_right, color: Color(0xFFCAE0D6)),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 22),
            _buttons(enabled: !busy),
          ],
        ],
      ),
    );
  }

  Widget _buttons({required bool enabled}) => LayoutBuilder(
    builder: (context, constraints) {
      final buttons = [
        FilledButton.icon(
          onPressed: enabled ? onStartText : null,
          style: FilledButton.styleFrom(
            backgroundColor: Colors.white,
            foregroundColor: replicaGreen,
          ),
          icon: const Icon(Icons.chat_bubble_outline, size: 18),
          label: const Text('텍스트로 대화'),
        ),
        OutlinedButton.icon(
          onPressed: enabled ? onStartVoice : null,
          style: OutlinedButton.styleFrom(
            foregroundColor: Colors.white,
            side: const BorderSide(color: Color(0xFF77A493)),
          ),
          icon: const Icon(Icons.mic_none, size: 18),
          label: const Text('음성으로 대화'),
        ),
      ];
      if (constraints.maxWidth < 340) {
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [buttons[0], const SizedBox(height: 8), buttons[1]],
        );
      }
      return Row(
        children: [
          Expanded(child: buttons[0]),
          const SizedBox(width: 10),
          Expanded(child: buttons[1]),
        ],
      );
    },
  );
}
