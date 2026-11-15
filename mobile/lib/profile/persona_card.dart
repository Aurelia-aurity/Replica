import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'persona_controller.dart';

/// SC-02 대화 상대 카드. Shows the partner name and the conversation buttons;
/// with several partners the name is a selector. Without a partner it offers
/// [onCreate] (SC-13) instead of the buttons.
class PersonaCard extends StatelessWidget {
  const PersonaCard({
    super.key,
    required this.controller,
    required this.onCreate,
    required this.onStartText,
    required this.onStartVoice,
    this.busy = false,
  });
  final PersonaController controller;
  final VoidCallback onCreate, onStartText, onStartVoice;
  final bool busy;

  static const _label = Text(
    '대화 상대',
    style: TextStyle(color: Color(0xFFCAE0D6), fontSize: 12),
  );
  static const _name = TextStyle(
    color: Colors.white,
    fontSize: 30,
    fontWeight: FontWeight.w700,
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
        controller.status == LoadStatus.idle ||
        controller.status == LoadStatus.loading;
    final failed = controller.status == LoadStatus.failed;
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        color: replicaGreen,
        borderRadius: BorderRadius.circular(22),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _label,
          const SizedBox(height: 12),
          if (loading)
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
            )
          else if (failed) ...[
            const Text('대화 상대를 불러오지 못했어요.', style: _muted),
            TextButton.icon(
              onPressed: controller.load,
              style: TextButton.styleFrom(foregroundColor: Colors.white),
              icon: const Icon(Icons.refresh, size: 18),
              label: const Text('다시 시도'),
            ),
          ] else if (persona == null) ...[
            const Text(
              '프로필을 만들어 주세요',
              style: TextStyle(
                color: Colors.white,
                fontSize: 22,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 12),
            const Text('대화할 상대의 이름이나 호칭을 정해 주세요.', style: _muted),
            const SizedBox(height: 22),
            FilledButton.icon(
              onPressed: onCreate,
              style: FilledButton.styleFrom(
                backgroundColor: Colors.white,
                foregroundColor: replicaGreen,
              ),
              icon: const Icon(Icons.add, size: 18),
              label: const Text('프로필 만들기'),
            ),
          ] else ...[
            if (controller.personas.length > 1)
              DropdownButtonHideUnderline(
                child: DropdownButton<String>(
                  key: const ValueKey('persona-selector'),
                  value: persona.id,
                  isExpanded: true,
                  dropdownColor: replicaGreen,
                  iconEnabledColor: Colors.white,
                  style: _name,
                  items: [
                    for (final item in controller.personas)
                      DropdownMenuItem(
                        value: item.id,
                        child: Text(
                          item.label,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                  ],
                  onChanged: busy
                      ? null
                      : (id) {
                          if (id != null) controller.select(id);
                        },
                ),
              )
            else
              Text(
                persona.label,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: _name,
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
