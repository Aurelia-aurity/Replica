import 'package:flutter/material.dart';

import '../history/conversation.dart';
import '../history/conversation_service.dart';
import '../history/history_screen.dart';
import '../shared/conversation_widgets.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({
    super.key,
    required this.history,
    this.accountEmail,
    this.onSignOut,
    this.onExpireSession,
  });
  final ConversationService history;
  final String? accountEmail;
  final VoidCallback? onSignOut;
  final VoidCallback? onExpireSession;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  bool _starting = false;
  int _tab = 0;
  late Future<List<Conversation>> _recent;

  @override
  void initState() {
    super.initState();
    _refreshRecent();
  }

  void _refreshRecent() {
    _recent = widget.history.list();
  }

  void _notice(String text) {
    ScaffoldMessenger.of(context).hideCurrentSnackBar();
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  void _account() {
    showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(widget.accountEmail ?? '계정'),
              const SizedBox(height: 16),
              FilledButton(
                onPressed: () {
                  Navigator.of(sheetContext).pop();
                  widget.onSignOut?.call();
                },
                child: const Text('로그아웃'),
              ),
              if (widget.onExpireSession != null)
                TextButton(
                  onPressed: () {
                    Navigator.of(sheetContext).pop();
                    widget.onExpireSession!();
                  },
                  child: const Text('세션 만료 확인 (미리보기)'),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _start({required bool voice}) async {
    if (_starting) return;
    setState(() => _starting = true);
    try {
      await startConversation(context, widget.history, voice: voice);
    } catch (_) {
      if (mounted) _notice('새 대화를 열지 못했어요. 대화 상대와 연결 상태를 확인해 주세요.');
    } finally {
      if (mounted) {
        setState(() {
          _starting = false;
          _refreshRecent();
        });
      }
    }
  }

  Future<void> _openRecent(Conversation item) async {
    if (_starting) return;
    setState(() => _starting = true);
    try {
      final latest = await widget.history.get(item.id);
      if (mounted) await openConversation(context, widget.history, latest);
    } catch (_) {
      if (mounted) _notice('대화를 불러오지 못했어요. 다시 시도해 주세요.');
    } finally {
      if (mounted) {
        setState(() {
          _starting = false;
          _refreshRecent();
        });
      }
    }
  }

  Widget _navigation() => NavigationBar(
    selectedIndex: _tab,
    backgroundColor: Colors.white,
    indicatorColor: const Color(0xFFE3EFEB),
    onDestinationSelected: (index) {
      if (index == 0 || index == 2) {
        setState(() {
          _tab = index;
          if (index == 0) _refreshRecent();
        });
      } else {
        _notice('${index == 1 ? '기록 관리' : '설정'} 기능은 고도화 단계에서 제공할 예정입니다.');
      }
    },
    destinations: const [
      NavigationDestination(
        icon: Icon(Icons.home_outlined),
        selectedIcon: Icon(Icons.home),
        label: '홈',
      ),
      NavigationDestination(icon: Icon(Icons.folder_outlined), label: '기록'),
      NavigationDestination(
        icon: Icon(Icons.chat_bubble_outline),
        selectedIcon: Icon(Icons.chat_bubble),
        label: '대화',
      ),
      NavigationDestination(icon: Icon(Icons.tune), label: '설정'),
    ],
  );

  @override
  Widget build(BuildContext context) {
    if (_tab == 2) {
      return HistoryScreen(
        service: widget.history,
        bottomNavigationBar: _navigation(),
      );
    }
    return Scaffold(
      backgroundColor: replicaBackground,
      appBar: AppBar(
        backgroundColor: replicaBackground,
        surfaceTintColor: Colors.transparent,
        title: const Text(
          '레플리카',
          style: TextStyle(fontWeight: FontWeight.w700),
        ),
        actions: [
          IconButton(
            tooltip: '계정',
            icon: const Icon(Icons.person_outline),
            onPressed: widget.onSignOut == null ? null : _account,
          ),
        ],
      ),
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 720),
          child: ListView(
            padding: const EdgeInsets.all(20),
            children: [
              Container(
                padding: const EdgeInsets.all(22),
                decoration: BoxDecoration(
                  color: replicaGreen,
                  borderRadius: BorderRadius.circular(22),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      '대화 상대',
                      style: TextStyle(color: Color(0xFFCAE0D6), fontSize: 12),
                    ),
                    const SizedBox(height: 12),
                    const Text(
                      '엄마',
                      style: TextStyle(
                        color: Colors.white,
                        fontSize: 30,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 12),
                    Text(
                      widget.history.isDemo
                          ? '데모 대화 · 말투 프로필과 기록은 아직 연결되지 않았어요'
                          : '대화를 시작해 보세요',
                      style: const TextStyle(
                        color: Color(0xFFDAEAE2),
                        height: 1.5,
                        fontSize: 13,
                      ),
                    ),
                    const SizedBox(height: 22),
                    LayoutBuilder(
                      builder: (context, constraints) {
                        final buttons = [
                          FilledButton.icon(
                            onPressed: _starting
                                ? null
                                : () => _start(voice: false),
                            style: FilledButton.styleFrom(
                              backgroundColor: Colors.white,
                              foregroundColor: replicaGreen,
                            ),
                            icon: const Icon(
                              Icons.chat_bubble_outline,
                              size: 18,
                            ),
                            label: const Text('텍스트로 대화'),
                          ),
                          OutlinedButton.icon(
                            onPressed: _starting
                                ? null
                                : () => _start(voice: true),
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
                            children: [
                              buttons[0],
                              const SizedBox(height: 8),
                              buttons[1],
                            ],
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
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 28),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  const Text(
                    '최근 대화',
                    style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
                  ),
                  TextButton(
                    onPressed: () => setState(() => _tab = 2),
                    child: const Text('전체 보기'),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              FutureBuilder<List<Conversation>>(
                future: _recent,
                builder: (context, snapshot) {
                  if (snapshot.hasError) {
                    return TextButton.icon(
                      onPressed: () => setState(_refreshRecent),
                      icon: const Icon(Icons.refresh),
                      label: const Text('최근 대화를 불러오지 못했어요. 다시 시도'),
                    );
                  }
                  if (!snapshot.hasData) {
                    return const Center(child: CircularProgressIndicator());
                  }
                  final items = [...snapshot.data!]
                    ..sort((a, b) => b.lastAt.compareTo(a.lastAt));
                  if (items.isEmpty) {
                    return const Padding(
                      padding: EdgeInsets.all(20),
                      child: Text(
                        '아직 나눈 대화가 없어요',
                        style: TextStyle(color: Color(0xFF68766F)),
                      ),
                    );
                  }
                  return Column(
                    children: [
                      for (final item in items.take(2))
                        Card(
                          color: Colors.white,
                          elevation: 0,
                          child: ListTile(
                            onTap: _starting ? null : () => _openRecent(item),
                            title: Text(
                              item.firstQuestion,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                            ),
                            subtitle: Text(
                              '${conversationTime(item.lastAt)} · ${item.lastVoice ? '음성' : '텍스트'}',
                            ),
                            trailing: const Icon(Icons.chevron_right),
                          ),
                        ),
                    ],
                  );
                },
              ),
              const SizedBox(height: 20),
              if (widget.history.isDemo)
                const Text(
                  '데모 모드 · 메시지는 서버로 전송되지 않습니다.',
                  style: TextStyle(fontSize: 12, color: Color(0xFF68766F)),
                ),
            ],
          ),
        ),
      ),
      bottomNavigationBar: _navigation(),
    );
  }
}
