import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';

/// Returns the acknowledgement time; null means the user closed the sheet.
Future<DateTime?> showAiDisclosure(BuildContext context, {required bool isDemo}) {
  return showModalBottomSheet<DateTime>(
    context: context,
    isScrollControlled: true,
    isDismissible: false,
    enableDrag: false,
    backgroundColor: Colors.white,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
    ),
    builder: (context) => _AiDisclosureSheet(isDemo: isDemo),
  );
}

class _AiDisclosureSheet extends StatefulWidget {
  const _AiDisclosureSheet({required this.isDemo});
  final bool isDemo;

  @override
  State<_AiDisclosureSheet> createState() => _AiDisclosureSheetState();
}

class _AiDisclosureSheetState extends State<_AiDisclosureSheet> {
  bool _acknowledged = false;

  Widget _item(IconData icon, String text) => Padding(
    padding: const EdgeInsets.only(bottom: 18),
    child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Container(
        padding: const EdgeInsets.all(9),
        decoration: BoxDecoration(color: aiAmberBackground, borderRadius: BorderRadius.circular(12)),
        child: Icon(icon, color: aiAmber, size: 20),
      ),
      const SizedBox(width: 12),
      Expanded(child: Text(text, style: const TextStyle(height: 1.6))),
    ]),
  );

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: false,
    child: SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * .9),
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
            const AiBadge(),
            const SizedBox(height: 16),
            const Text('대화를 시작하기 전에', style: TextStyle(fontSize: 24, fontWeight: FontWeight.w700)),
            const SizedBox(height: 24),
            _item(Icons.chat_bubble_outline, '응답은 올려 주신 기록을 바탕으로 AI가 만든 문장이에요. 그분이 실제로 한 말이 아니에요.'),
            _item(Icons.search, '기록에서 찾을 수 없는 내용은 지어내지 않고 “기록에서 찾을 수 없어요”라고 답하는 것을 원칙으로 해요.'),
            _item(Icons.graphic_eq, '음성 응답은 합성한 목소리예요. 재생하기 전에 다시 알려 드려요.'),
            if (widget.isDemo)
              const Padding(
                padding: EdgeInsets.only(bottom: 16),
                child: Text('현재는 데모예요. 실제 기록 검색·AI 서버 연결·음성 녹음 및 재생은 제공하지 않습니다.',
                  style: TextStyle(color: Color(0xFF68766F), fontSize: 12, height: 1.5)),
              ),
            DecoratedBox(
              decoration: BoxDecoration(color: replicaBackground, borderRadius: BorderRadius.circular(12)),
              child: CheckboxListTile(
                value: _acknowledged,
                onChanged: (value) => setState(() => _acknowledged = value ?? false),
                controlAffinity: ListTileControlAffinity.leading,
                title: const Text('위 내용을 이해했어요'),
                contentPadding: const EdgeInsets.symmetric(horizontal: 8),
                activeColor: replicaGreen,
              ),
            ),
            const SizedBox(height: 20),
            Row(children: [
              Expanded(child: OutlinedButton(
                onPressed: () => Navigator.of(context).pop(),
                child: const Text('닫기'),
              )),
              const SizedBox(width: 12),
              Expanded(flex: 2, child: FilledButton(
                onPressed: _acknowledged ? () => Navigator.of(context).pop(DateTime.now()) : null,
                child: const Text('대화 시작'),
              )),
            ]),
          ]),
        ),
      ),
    ),
  );
}
