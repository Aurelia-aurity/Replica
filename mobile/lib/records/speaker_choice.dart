import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'text_analysis.dart';

/// SC-04 speaker choice: whose speech to learn and who provided the file.
///
/// Shared by the upload screen and the speaker check of a record waiting
/// after the upload. A lone speaker keeps the provider area visible but
/// disabled, and is chosen only by the user's own tap.
List<Widget> speakerChoiceSections({
  required List<ChatSpeaker> speakers,
  required String? target,
  required String? provider,
  required bool locked,
  required bool sendSelection,
  required ValueChanged<String> onTarget,
  required ValueChanged<String> onProvider,
  String singleNotice =
      '이 화자가 대화 상대일 때만 선택해 주세요. 대화 상대가 아니라면 선택하지 말고 다른 파일을 골라 주세요.',
}) {
  final single = speakers.length == 1;
  Widget group({
    required String title,
    required String keyPrefix,
    required String? selected,
    required String? blocked,
    required ValueChanged<String> onSelect,
    bool disabled = false,
  }) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(
        title,
        style: TextStyle(
          fontWeight: FontWeight.w600,
          color: disabled ? const Color(0xFF9AA5A0) : const Color(0xFF4C5C54),
        ),
      ),
      const SizedBox(height: 12),
      Container(
        decoration: BoxDecoration(
          color: Colors.white,
          border: Border.all(color: replicaBorder),
          borderRadius: BorderRadius.circular(16),
        ),
        clipBehavior: Clip.antiAlias,
        child: Column(
          children: [
            for (var i = 0; i < speakers.length; i++) ...[
              if (i > 0) const Divider(height: 1, color: replicaBorder),
              _SpeakerTile(
                tileKey: ValueKey('$keyPrefix-${speakers[i].name}'),
                speaker: speakers[i],
                selected: selected == speakers[i].name,
                enabled: !locked && !disabled && blocked != speakers[i].name,
                onTap: () => onSelect(speakers[i].name),
              ),
            ],
          ],
        ),
      ),
    ],
  );
  return [
    const SizedBox(height: 24),
    group(
      title: '누구의 말투를 반영할까요?',
      keyPrefix: 'speaker-target',
      selected: target,
      blocked: provider,
      onSelect: onTarget,
    ),
    const SizedBox(height: 10),
    if (single)
      Text(
        singleNotice,
        key: const ValueKey('speaker-single-notice'),
        style: const TextStyle(
          fontSize: 12,
          height: 1.6,
          fontWeight: FontWeight.w600,
          color: Color(0xFF4C5C54),
        ),
      )
    else
      const Text(
        '고르지 않은 사람의 메시지는 문맥으로만 쓰고, 다른 사람의 개인 정보는 저장 전에 가려요.',
        style: speakerMutedText,
      ),
    const SizedBox(height: 24),
    group(
      title: '이 기록을 제공한 사람은 누구인가요?',
      keyPrefix: 'speaker-provider',
      selected: single ? null : provider,
      blocked: target,
      onSelect: onProvider,
      disabled: single,
    ),
    if (!single || !sendSelection) ...[
      const SizedBox(height: 10),
      Text(switch ((single, sendSelection)) {
        (true, _) =>
          '화자 목록은 이 기기에서만 확인하며, 선택 내용을 서버에 전달하는 방식은 아직 합의 전이라 이번에는 전송하지 않아요.',
        (false, true) =>
          '말투를 반영할 사람과 제공한 사람은 서로 달라야 해요. 제공자는 본인 동의 확인에 함께 저장돼요.',
        (false, false) => '말투를 반영할 사람과 제공한 사람은 서로 달라야 해요. 화자 목록은 이 기기에서만 확인하며, 선택 내용을 서버에 전달하는 방식은 아직 합의 전이라 이번에는 전송하지 않아요.',
      }, style: speakerMutedText),
    ],
  ];
}

const speakerMutedText = TextStyle(
  fontSize: 12,
  height: 1.6,
  color: Color(0xFF75817B),
);

class _SpeakerTile extends StatelessWidget {
  const _SpeakerTile({
    required this.tileKey,
    required this.speaker,
    required this.selected,
    required this.enabled,
    required this.onTap,
  });
  final Key tileKey;
  final ChatSpeaker speaker;
  final bool selected;
  final bool enabled;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => Semantics(
    inMutuallyExclusiveGroup: true,
    checked: selected,
    enabled: enabled,
    child: InkWell(
      key: tileKey,
      onTap: enabled ? onTap : null,
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 52),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          child: Row(
            children: [
              Icon(
                selected
                    ? Icons.radio_button_checked
                    : Icons.radio_button_unchecked,
                color: !enabled && !selected
                    ? const Color(0xFFB9C2BD)
                    : replicaGreen,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Text(
                  speaker.name,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: 15,
                    color: enabled || selected
                        ? const Color(0xFF17201C)
                        : const Color(0xFF9AA5A0),
                  ),
                ),
              ),
              if (speaker.messages != null) ...[
                const SizedBox(width: 8),
                Text(
                  '메시지 ${formatCount(speaker.messages!)}개',
                  style: speakerMutedText,
                ),
              ],
            ],
          ),
        ),
      ),
    ),
  );
}
