import 'package:flutter/material.dart';

import 'chat_service.dart';
import 'ai_disclosure_sheet.dart';
import 'voice_screen.dart';
import '../shared/conversation_widgets.dart';

const _green = replicaGreen;
const _background = replicaBackground;
const _border = replicaBorder;
const _amber = aiAmber;
const _amberBackground = aiAmberBackground;

class ChatScreen extends StatefulWidget {
  const ChatScreen({
    super.key,
    required this.service,
    this.conversationName = '엄마',
    this.initialVoiceMode = false,
    this.acknowledgedAt,
  });
  final ChatService service;
  final String conversationName;
  final bool initialVoiceMode;
  /// Session acknowledgement; persisted only when an API contract is added.
  final DateTime? acknowledgedAt;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _input = TextEditingController();
  final _scroll = ScrollController();
  final _messages = <({String text, bool isUser})>[];
  bool _loading = false;
  String? _failedMessage;
  late bool _voiceMode;

  @override
  void initState() {
    super.initState();
    _voiceMode = widget.initialVoiceMode;
  }

  String? _latestText({required bool isUser}) {
    for (final message in _messages.reversed) {
      if (message.isUser == isUser) return message.text;
    }
    return null;
  }

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _scrollToLatest() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && _scroll.hasClients) {
        _scroll.animateTo(
          _scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
        );
      }
    });
  }

  Future<void> _send({String? retry}) async {
    final text = retry ?? _input.text.trim();
    if (_loading || text.isEmpty || (_failedMessage != null && retry == null)) {
      return;
    }
    setState(() {
      _loading = true;
      _failedMessage = null;
      if (retry == null) {
        _messages.add((text: text, isUser: true));
        _input.clear();
      }
    });
    _scrollToLatest();
    try {
      final response = await widget.service
          .reply(text)
          .timeout(const Duration(seconds: 30));
      if (!mounted) return;
      if (response.trim().isEmpty) throw StateError('Empty response');
      setState(() => _messages.add((text: response, isUser: false)));
    } catch (_) {
      if (!mounted) return;
      setState(() => _failedMessage = text);
    } finally {
      if (mounted) {
        setState(() => _loading = false);
        _scrollToLatest();
      }
    }
  }

  void _notice(String message) {
    ScaffoldMessenger.of(context).hideCurrentSnackBar();
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _listen() async {
    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('합성한 목소리예요'),
        content: const Text(
          'AI 응답을 합성 음성으로 읽는 기능입니다. 현재 데모에서는 음성 재생을 제공하지 않습니다.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('확인'),
          ),
        ],
      ),
    );
  }

  Widget _bubble(String text, {required bool isUser, Widget? footer}) {
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: FractionallySizedBox(
        widthFactor: .88,
        child: Column(
          crossAxisAlignment: isUser
              ? CrossAxisAlignment.end
              : CrossAxisAlignment.start,
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 13),
              decoration: BoxDecoration(
                color: isUser ? _green : Colors.white,
                border: isUser ? null : Border.all(color: _border),
                borderRadius: BorderRadius.only(
                  topLeft: const Radius.circular(22),
                  topRight: const Radius.circular(22),
                  bottomLeft: Radius.circular(isUser ? 22 : 5),
                  bottomRight: Radius.circular(isUser ? 5 : 22),
                ),
              ),
              child: SelectableText(
                text,
                style: TextStyle(
                  fontSize: 16,
                  height: 1.55,
                  color: isUser ? Colors.white : const Color(0xFF26332D),
                ),
              ),
            ),
            if (footer != null)
              Padding(padding: const EdgeInsets.only(top: 6), child: footer),
          ],
        ),
      ),
    );
  }

  Widget _aiActions() {
    return Wrap(
      spacing: 8,
      runSpacing: 4,
      crossAxisAlignment: WrapCrossAlignment.center,
      children: [
        const AiBadge(),
        OutlinedButton.icon(
          onPressed: _listen,
          style: OutlinedButton.styleFrom(
            foregroundColor: const Color(0xFF57645D),
            backgroundColor: Colors.white,
            side: const BorderSide(color: _border),
            minimumSize: const Size(0, 40),
            padding: const EdgeInsets.symmetric(horizontal: 12),
          ),
          icon: const Icon(Icons.volume_up_outlined, size: 17),
          label: const Text('듣기'),
        ),
      ],
    );
  }

  Widget _failureActions() {
    return Wrap(
      spacing: 4,
      children: [
        TextButton.icon(
          onPressed: () => _send(retry: _failedMessage),
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('다시 시도'),
        ),
        TextButton(
          onPressed: () => setState(() {
            _input.text = _failedMessage!;
            _messages.removeLast();
            _failedMessage = null;
          }),
          child: const Text('메시지 수정'),
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_voiceMode) {
      return VoiceScreen(
        conversationName: widget.conversationName,
        onKeyboard: () => setState(() => _voiceMode = false),
        lastQuestion: _latestText(isUser: true),
        lastReply: _latestText(isUser: false),
      );
    }
    return Scaffold(
      backgroundColor: _background,
      appBar: AppBar(
        backgroundColor: Colors.white,
        surfaceTintColor: Colors.transparent,
        title: Text(
          widget.conversationName,
          style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
        ),
        leading: IconButton(
          tooltip: '뒤로',
          icon: const Icon(Icons.chevron_left),
          onPressed: () async {
            if (!await Navigator.of(context).maybePop() && mounted) {
              _notice('홈에서 새 대화를 시작할 수 있어요.');
            }
          },
        ),
        actions: [
          IconButton(
            tooltip: 'AI 생성 안내 다시 보기',
            icon: const Icon(Icons.more_horiz),
            onPressed: () => showAiDisclosure(context, isDemo: widget.service.isDemo),
          ),
        ],
      ),
      body: SafeArea(
        top: false,
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: Column(
              children: [
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.symmetric(
                    horizontal: 12,
                    vertical: 11,
                  ),
                  color: _amberBackground,
                  child: const Row(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Icon(Icons.info_outline, size: 16, color: _amber),
                      SizedBox(width: 6),
                      Flexible(
                        child: Text(
                          '기록을 바탕으로 AI가 만든 대화예요',
                          style: TextStyle(
                            color: _amber,
                            fontSize: 12,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
                Expanded(
                  child: ListView(
                    controller: _scroll,
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 18,
                    ),
                    children: [
                      if (widget.service.isDemo)
                        const Padding(
                          padding: EdgeInsets.only(bottom: 20),
                          child: Text(
                            '데모 모드 · 메시지는 서버로 전송되지 않습니다.',
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              fontSize: 12,
                              color: Color(0xFF68766F),
                            ),
                          ),
                        ),
                      if (_messages.isEmpty)
                        const Padding(
                          padding: EdgeInsets.symmetric(vertical: 36),
                          child: Column(
                            children: [
                              Icon(
                                Icons.chat_bubble_outline,
                                size: 36,
                                color: _green,
                              ),
                              SizedBox(height: 16),
                              Text(
                                '어떤 이야기를 나누고 싶나요?',
                                textAlign: TextAlign.center,
                              ),
                              SizedBox(height: 8),
                              Text(
                                '메시지를 입력해 대화를 시작하세요.',
                                textAlign: TextAlign.center,
                                style: TextStyle(
                                  fontSize: 13,
                                  color: Color(0xFF68766F),
                                ),
                              ),
                            ],
                          ),
                        ),
                      for (var index = 0; index < _messages.length; index++)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 16),
                          child: _bubble(
                            _messages[index].text,
                            isUser: _messages[index].isUser,
                            footer: !_messages[index].isUser
                                ? _aiActions()
                                : _failedMessage != null && index == _messages.length - 1
                                    ? IconButton(
                                        tooltip: '다시 보내기',
                                        onPressed: () => _send(retry: _failedMessage),
                                        icon: const Icon(Icons.refresh, color: _green),
                                      )
                                    : null,
                          ),
                        ),
                      if (_loading)
                        Semantics(
                          liveRegion: true,
                          label: '응답을 만드는 중',
                          child: Align(
                            alignment: Alignment.centerLeft,
                            child: Container(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 16,
                                vertical: 14,
                              ),
                              decoration: BoxDecoration(
                                color: Colors.white,
                                border: Border.all(color: _border),
                                borderRadius: BorderRadius.circular(20),
                              ),
                              child: const Row(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Text(
                                    '•••',
                                    style: TextStyle(
                                      color: Color(0xFFA9B5AF),
                                      fontSize: 20,
                                      letterSpacing: 2,
                                    ),
                                  ),
                                  SizedBox(width: 6),
                                  Flexible(
                                    child: Text(
                                      '응답을 만드는 중',
                                      style: TextStyle(
                                        color: Color(0xFF68766F),
                                      ),
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ),
                        ),
                      if (_failedMessage != null)
                        Semantics(
                          liveRegion: true,
                          child: _bubble(
                            '응답을 받지 못했어요.',
                            isUser: false,
                            footer: _failureActions(),
                          ),
                        ),
                    ],
                  ),
                ),
                Container(
                  decoration: const BoxDecoration(
                    color: Colors.white,
                    border: Border(top: BorderSide(color: _border)),
                  ),
                  padding: const EdgeInsets.fromLTRB(12, 10, 12, 12),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.end,
                    children: [
                      IconButton.filledTonal(
                        tooltip: '음성 대화',
                        style: IconButton.styleFrom(
                          backgroundColor: const Color(0xFFE3EFEB),
                          foregroundColor: _green,
                        ),
                        onPressed: _loading || _failedMessage != null
                            ? null
                            : () {
                                FocusScope.of(context).unfocus();
                                setState(() => _voiceMode = true);
                              },
                        icon: const Icon(Icons.mic_none),
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: TextField(
                          controller: _input,
                          readOnly: _loading || _failedMessage != null,
                          minLines: 1,
                          maxLines: MediaQuery.viewInsetsOf(context).bottom > 0
                              ? 2
                              : 4,
                          maxLength: 2000,
                          style: const TextStyle(fontSize: 15),
                          decoration: InputDecoration(
                            hintText: '메시지 입력',
                            counterText: '',
                            filled: true,
                            fillColor: _background,
                            contentPadding: const EdgeInsets.symmetric(
                              horizontal: 16,
                              vertical: 12,
                            ),
                            border: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(28),
                              borderSide: const BorderSide(color: _border),
                            ),
                            enabledBorder: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(28),
                              borderSide: const BorderSide(color: _border),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      ValueListenableBuilder<TextEditingValue>(
                        valueListenable: _input,
                        builder: (context, value, child) => IconButton.filled(
                          tooltip: '메시지 전송',
                          style: IconButton.styleFrom(
                            backgroundColor: _green,
                            foregroundColor: Colors.white,
                          ),
                          onPressed:
                              _loading ||
                                  _failedMessage != null ||
                                  value.text.trim().isEmpty
                              ? null
                              : () => _send(),
                          icon: const Icon(Icons.send_outlined),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
