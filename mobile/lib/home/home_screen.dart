import 'package:flutter/material.dart';

import '../chat/ai_disclosure_sheet.dart';
import '../chat/chat_screen.dart';
import '../chat/chat_service.dart';
import '../shared/conversation_widgets.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key, required this.service});
  final ChatService service;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  bool _starting = false;

  void _notice(String text) {
    ScaffoldMessenger.of(context).hideCurrentSnackBar();
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _start({required bool voice}) async {
    if (_starting) return;
    setState(() => _starting = true);
    try {
      final acknowledgedAt = await showAiDisclosure(context, isDemo: widget.service.isDemo);
      if (!mounted || acknowledgedAt == null) return;
      await Navigator.of(context).push<void>(MaterialPageRoute(
        builder: (context) => ChatScreen(
          service: widget.service,
          initialVoiceMode: voice,
          acknowledgedAt: acknowledgedAt,
        ),
      ));
    } finally {
      if (mounted) setState(() => _starting = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: replicaBackground,
    appBar: AppBar(
      backgroundColor: replicaBackground,
      surfaceTintColor: Colors.transparent,
      title: const Text('레플리카', style: TextStyle(fontWeight: FontWeight.w700)),
      actions: [IconButton(
        tooltip: '계정', icon: const Icon(Icons.person_outline),
        onPressed: () => _notice('계정 기능은 고도화 단계에서 제공할 예정입니다.'),
      )],
    ),
    body: Center(child: ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: 720),
      child: ListView(padding: const EdgeInsets.all(20), children: [
        Container(
          padding: const EdgeInsets.all(22),
          decoration: BoxDecoration(color: replicaGreen, borderRadius: BorderRadius.circular(22)),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Text('대화 상대', style: TextStyle(color: Color(0xFFCAE0D6), fontSize: 12)),
            const SizedBox(height: 12),
            const Text('엄마', style: TextStyle(color: Colors.white, fontSize: 30, fontWeight: FontWeight.w700)),
            const SizedBox(height: 12),
            Text(widget.service.isDemo ? '데모 대화 · 말투 프로필과 기록은 아직 연결되지 않았어요' : '대화를 시작해 보세요',
              style: const TextStyle(color: Color(0xFFDAEAE2), height: 1.5, fontSize: 13)),
            const SizedBox(height: 22),
            LayoutBuilder(builder: (context, constraints) {
              final buttons = [
                FilledButton.icon(
                  onPressed: _starting ? null : () => _start(voice: false),
                  style: FilledButton.styleFrom(backgroundColor: Colors.white, foregroundColor: replicaGreen),
                  icon: const Icon(Icons.chat_bubble_outline, size: 18), label: const Text('텍스트로 대화'),
                ),
                OutlinedButton.icon(
                  onPressed: _starting ? null : () => _start(voice: true),
                  style: OutlinedButton.styleFrom(foregroundColor: Colors.white, side: const BorderSide(color: Color(0xFF77A493))),
                  icon: const Icon(Icons.mic_none, size: 18), label: const Text('음성으로 대화'),
                ),
              ];
              if (constraints.maxWidth < 340) {
                return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [buttons[0], const SizedBox(height: 8), buttons[1]]);
              }
              return Row(children: [Expanded(child: buttons[0]), const SizedBox(width: 10), Expanded(child: buttons[1])]);
            }),
          ]),
        ),
        const SizedBox(height: 28),
        const Text('최근 대화', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.all(20),
          decoration: BoxDecoration(color: Colors.white, border: Border.all(color: replicaBorder), borderRadius: BorderRadius.circular(18)),
          child: const Text('대화 이력은 아직 제공하지 않습니다.\n데모 대화는 화면을 나가면 저장되지 않아요.',
            style: TextStyle(color: Color(0xFF68766F), height: 1.6)),
        ),
        const SizedBox(height: 20),
        if (widget.service.isDemo)
          const Text('데모 모드 · 메시지는 서버로 전송되지 않습니다.', style: TextStyle(fontSize: 12, color: Color(0xFF68766F))),
      ]),
    )),
    bottomNavigationBar: NavigationBar(
      selectedIndex: 0,
      backgroundColor: Colors.white,
      indicatorColor: const Color(0xFFE3EFEB),
      onDestinationSelected: (index) {
        if (index == 0) return;
        const names = ['홈', '기록 관리', '대화 이력', '설정'];
        _notice('${names[index]} 기능은 고도화 단계에서 제공할 예정입니다.');
      },
      destinations: const [
        NavigationDestination(icon: Icon(Icons.home_outlined), selectedIcon: Icon(Icons.home), label: '홈'),
        NavigationDestination(icon: Icon(Icons.folder_outlined), label: '기록'),
        NavigationDestination(icon: Icon(Icons.chat_bubble_outline), label: '대화'),
        NavigationDestination(icon: Icon(Icons.tune), label: '설정'),
      ],
    ),
  );
}
