import 'package:flutter/material.dart';

import '../chat/ai_disclosure_sheet.dart';
import '../chat/chat_screen.dart';
import '../shared/conversation_widgets.dart';
import 'conversation.dart';
import 'conversation_service.dart';

Future<void> openConversation(
  BuildContext context,
  ConversationService service,
  Conversation conversation, {
  bool? voice,
}) => Navigator.of(context).push<void>(
  MaterialPageRoute(
    builder: (_) => ChatScreen(
      service: ConversationChatService(service, conversation.id),
      conversationName: service.isDemo ? '엄마' : '대화',
      initialVoiceMode: voice ?? conversation.lastVoice,
      acknowledgedAt: conversation.acknowledgedAt,
      initialMessages: conversation.messages,
      initialReplies: conversation.replyMetadata,
      historyNotice: conversation.hasFailedTurns
          ? '이전에 처리하지 못한 메시지가 있어요. 새 메시지로 대화를 이어갈 수 있어요.'
          : null,
      blockedReason: conversation.pending
          ? '아직 처리 중인 메시지가 있어요. 이력으로 돌아가 새로고침해 주세요.'
          : null,
    ),
  ),
);

Future<void> startConversation(
  BuildContext context,
  ConversationService service, {
  bool voice = false,
}) async {
  final acknowledgedAt = await showAiDisclosure(
    context,
    isDemo: service.isDemo,
  );
  if (acknowledgedAt == null || !context.mounted) return;
  final conversation = await service.create(acknowledgedAt, voice: voice);
  if (!context.mounted) return;
  await openConversation(context, service, conversation, voice: voice);
}

class HistoryScreen extends StatefulWidget {
  const HistoryScreen({
    super.key,
    required this.service,
    this.bottomNavigationBar,
  });
  final ConversationService service;
  final Widget? bottomNavigationBar;
  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen> {
  List<Conversation> _items = [];
  bool _loading = true, _failed = false, _starting = false;
  bool _confirming = false;
  String? _opening;
  final _deleting = <String>{};
  int _request = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final request = ++_request;
    setState(() {
      _loading = true;
      _failed = false;
    });
    try {
      final items = await widget.service.list();
      if (!mounted || request != _request) return;
      items.sort((a, b) {
        final date = b.lastAt.compareTo(a.lastAt);
        return date == 0 ? b.id.compareTo(a.id) : date;
      });
      setState(() => _items = items);
    } catch (_) {
      if (mounted && request == _request) setState(() => _failed = true);
    } finally {
      if (mounted && request == _request) setState(() => _loading = false);
    }
  }

  void _notice(String text, {VoidCallback? retry}) {
    ScaffoldMessenger.of(context).hideCurrentSnackBar();
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(text),
        action: retry == null
            ? null
            : SnackBarAction(label: '다시 시도', onPressed: retry),
      ),
    );
  }

  Future<void> _start() async {
    if (_starting || _opening != null) return;
    setState(() => _starting = true);
    try {
      final voice = await showModalBottomSheet<bool>(
        context: context,
        backgroundColor: Colors.white,
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        ),
        builder: (sheetContext) => SafeArea(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const Text(
                  '어떤 방식으로 대화할까요?',
                  style: TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
                ),
                const SizedBox(height: 24),
                FilledButton.icon(
                  onPressed: () => Navigator.pop(sheetContext, false),
                  icon: const Icon(Icons.chat_bubble_outline),
                  label: const Text('텍스트로 대화'),
                ),
                const SizedBox(height: 12),
                OutlinedButton.icon(
                  onPressed: () => Navigator.pop(sheetContext, true),
                  icon: const Icon(Icons.mic_none),
                  label: const Text('음성으로 대화'),
                ),
                const SizedBox(height: 12),
                TextButton(
                  onPressed: () => Navigator.pop(sheetContext),
                  child: const Text('취소'),
                ),
              ],
            ),
          ),
        ),
      );
      if (voice == null || !mounted) return;
      await startConversation(context, widget.service, voice: voice);
      if (mounted) await _load();
    } catch (_) {
      if (mounted) _notice('새 대화를 열지 못했어요. 대화 상대와 연결 상태를 확인해 주세요.');
    } finally {
      if (mounted) setState(() => _starting = false);
    }
  }

  Future<void> _open(Conversation item) async {
    if (_opening != null || _starting || _deleting.isNotEmpty) return;
    setState(() => _opening = item.id);
    try {
      final latest = await widget.service.get(item.id);
      if (!mounted) return;
      await openConversation(context, widget.service, latest);
      if (mounted) await _load();
    } catch (_) {
      if (mounted) _notice('대화를 불러오지 못했어요.', retry: () => _open(item));
    } finally {
      if (mounted) setState(() => _opening = null);
    }
  }

  Future<void> _delete(Conversation item) async {
    if (_confirming ||
        _deleting.isNotEmpty ||
        _opening != null ||
        _starting ||
        _loading) {
      return;
    }
    _confirming = true;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('대화를 삭제할까요?'),
        content: Text(
          '“${item.firstQuestion}”\n\n이 대화와 메시지가 삭제됩니다. 되돌릴 수 없어요. 업로드한 원본 기록은 유지됩니다.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('취소'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(context, true),
            style: TextButton.styleFrom(
              foregroundColor: const Color(0xFFB42318),
            ),
            child: const Text('삭제'),
          ),
        ],
      ),
    );
    _confirming = false;
    if (confirmed != true || !mounted) return;
    // Supersede any in-flight refresh so it cannot resurrect a deleted item.
    _request++;
    setState(() {
      _deleting.add(item.id);
      _loading = false;
    });
    try {
      await widget.service.delete(item.id);
      if (!mounted) return;
      setState(() => _items.removeWhere((entry) => entry.id == item.id));
      _notice('대화가 삭제되었어요.');
    } catch (_) {
      if (mounted) {
        _notice('대화를 삭제하지 못했어요. 이력은 그대로 유지돼요.', retry: () => _delete(item));
      }
    } finally {
      if (mounted) setState(() => _deleting.remove(item.id));
    }
  }

  @override
  Widget build(BuildContext context) {
    final now = DateTime.now();
    String? previousGroup;
    return Scaffold(
      backgroundColor: replicaBackground,
      appBar: AppBar(
        backgroundColor: replicaBackground,
        surfaceTintColor: Colors.transparent,
        title: const Text('대화', style: TextStyle(fontWeight: FontWeight.w700)),
        actions: [
          IconButton(
            tooltip: '대화 이력 새로고침',
            onPressed: _loading || _deleting.isNotEmpty ? null : _load,
            icon: const Icon(Icons.refresh),
          ),
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: FilledButton.icon(
              onPressed: _starting || _opening != null || _deleting.isNotEmpty
                  ? null
                  : _start,
              icon: const Icon(Icons.add, size: 18),
              label: const Text('새 대화'),
            ),
          ),
        ],
      ),
      bottomNavigationBar: widget.bottomNavigationBar,
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: RefreshIndicator(
              onRefresh: () async {
                if (_deleting.isEmpty) await _load();
              },
              child: ListView(
                padding: const EdgeInsets.all(20),
                physics: const AlwaysScrollableScrollPhysics(),
                children: [
                  if (widget.service.isDemo)
                    const Padding(
                      padding: EdgeInsets.only(bottom: 20),
                      child: Text(
                        '미리보기 · 대화는 이번 로그인 동안만 보관돼요.',
                        style: TextStyle(
                          fontSize: 12,
                          color: Color(0xFF68766F),
                        ),
                      ),
                    ),
                  if (_loading)
                    const Padding(
                      padding: EdgeInsets.all(32),
                      child: Center(
                        child: CircularProgressIndicator(
                          semanticsLabel: '대화 이력 불러오는 중',
                        ),
                      ),
                    )
                  else if (_failed)
                    _state(
                      Icons.cloud_off_outlined,
                      '대화 이력을 불러오지 못했어요.',
                      TextButton.icon(
                        onPressed: _load,
                        icon: const Icon(Icons.refresh),
                        label: const Text('다시 시도'),
                      ),
                    )
                  else if (_items.isEmpty)
                    _state(
                      Icons.chat_bubble_outline,
                      '아직 나눈 대화가 없어요',
                      FilledButton.icon(
                        onPressed: _starting ? null : _start,
                        icon: const Icon(Icons.add),
                        label: const Text('새 대화'),
                      ),
                    )
                  else
                    for (final item in _items) ...[
                      if (previousGroup !=
                          conversationDateGroup(item.lastAt, now))
                        Padding(
                          padding: const EdgeInsets.fromLTRB(4, 12, 4, 10),
                          child: Text(
                            previousGroup = conversationDateGroup(
                              item.lastAt,
                              now,
                            ),
                            style: const TextStyle(
                              color: Color(0xFF68766F),
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ),
                      Padding(
                        padding: const EdgeInsets.only(bottom: 10),
                        child: ConversationHistoryTile(
                          key: ValueKey(item.id),
                          item: item,
                          busy:
                              _deleting.contains(item.id) ||
                              _opening == item.id,
                          onOpen: () => _open(item),
                          onDelete: () => _delete(item),
                        ),
                      ),
                    ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _state(IconData icon, String title, Widget action) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 64),
    child: Column(
      children: [
        Icon(icon, size: 40, color: replicaGreen),
        const SizedBox(height: 20),
        Text(title, textAlign: TextAlign.center),
        const SizedBox(height: 20),
        action,
      ],
    ),
  );
}

class ConversationHistoryTile extends StatefulWidget {
  const ConversationHistoryTile({
    super.key,
    required this.item,
    required this.onOpen,
    required this.onDelete,
    this.busy = false,
  });
  final Conversation item;
  final VoidCallback onOpen, onDelete;
  final bool busy;
  @override
  State<ConversationHistoryTile> createState() =>
      _ConversationHistoryTileState();
}

class _ConversationHistoryTileState extends State<ConversationHistoryTile> {
  bool _revealed = false;
  @override
  Widget build(BuildContext context) => ClipRRect(
    borderRadius: BorderRadius.circular(18),
    child: Stack(
      children: [
        if (_revealed)
          Positioned.fill(
            child: Align(
              alignment: Alignment.centerRight,
              child: SizedBox(
                width: 84,
                height: double.infinity,
                child: Material(
                  color: const Color(0xFFB42318),
                  child: InkWell(
                    onTap: widget.busy
                        ? null
                        : () {
                            setState(() => _revealed = false);
                            widget.onDelete();
                          },
                    child: const Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      children: [
                        Icon(Icons.delete_outline, color: Colors.white),
                        SizedBox(height: 4),
                        Text('삭제', style: TextStyle(color: Colors.white)),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        GestureDetector(
          onHorizontalDragUpdate: widget.busy
              ? null
              : (details) {
                  if (details.delta.dx.abs() > 1) {
                    setState(() => _revealed = details.delta.dx < 0);
                  }
                },
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 160),
            transform: Matrix4.translationValues(_revealed ? -84 : 0, 0, 0),
            child: Material(
              color: Colors.white,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(18),
                side: const BorderSide(color: replicaBorder),
              ),
              child: ListTile(
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 8,
                ),
                onTap: widget.busy
                    ? null
                    : () {
                        if (_revealed) {
                          setState(() => _revealed = false);
                        } else {
                          widget.onOpen();
                        }
                      },
                leading: CircleAvatar(
                  backgroundColor: const Color(0xFFE3EFEB),
                  foregroundColor: replicaGreen,
                  child: Icon(
                    widget.item.lastVoice
                        ? Icons.mic_none
                        : Icons.chat_bubble_outline,
                    size: 20,
                  ),
                ),
                title: Text(
                  widget.item.firstQuestion,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(
                    fontSize: 15,
                    fontWeight: FontWeight.w500,
                  ),
                ),
                subtitle: Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text(
                    '${conversationTime(widget.item.lastAt)}\n${widget.item.lastVoice ? '음성' : '텍스트'} · 메시지 ${widget.item.messageCount}개',
                    style: const TextStyle(
                      color: Color(0xFF68766F),
                      fontSize: 12,
                      height: 1.5,
                    ),
                  ),
                ),
                trailing: widget.busy
                    ? const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : IconButton(
                        tooltip: '대화 삭제',
                        onPressed: () {
                          setState(() => _revealed = false);
                          widget.onDelete();
                        },
                        icon: const Icon(
                          Icons.more_horiz,
                          color: Color(0xFF68766F),
                        ),
                      ),
              ),
            ),
          ),
        ),
      ],
    ),
  );
}
