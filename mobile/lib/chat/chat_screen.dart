import 'dart:async';

import 'package:flutter/material.dart';

import 'chat_service.dart';
import 'ai_disclosure_sheet.dart';
import 'voice_screen.dart';
import 'reply_audio_player.dart';
import 'reply_evidence.dart';
import 'voice_recorder.dart';
import '../shared/conversation_widgets.dart';
import '../services/backend_service.dart';

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
    this.initialMessages = const [],
    this.blockedReason,
    this.historyNotice,
    this.initialReplies = const {},
    this.audioPlayer,
    this.voiceRecorder,
    this.voiceAudioPlayer,
  });
  final ChatService service;
  final String conversationName;
  final bool initialVoiceMode;
  final List<({String text, bool isUser})> initialMessages;
  final String? blockedReason;
  final String? historyNotice;
  final Map<int, ChatReply> initialReplies;
  final ReplyAudioPlayer? audioPlayer;
  final VoiceRecorder? voiceRecorder;
  final ReplyAudioPlayer? voiceAudioPlayer;

  /// AI acknowledgement stored with the conversation by its service.
  final DateTime? acknowledgedAt;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> with WidgetsBindingObserver {
  final _input = TextEditingController();
  final _scroll = ScrollController();
  final _messages = <({String text, bool isUser})>[];
  bool _loading = false;
  String? _failedMessage;
  final _replyDetails = <int, ChatReply>{};
  final _speechCache = <int, SpeechAudio>{};
  final _speechFailed = <int>{};
  late final ReplyAudioPlayer _audio;
  late final StreamSubscription<void> _audioComplete;
  late final StreamSubscription<Object> _audioErrors;
  int? _speechBusy, _playing;
  int _speechGeneration = 0;
  bool _speechPrompt = false, _voiceAcknowledged = false;
  bool _retryable = true;
  String _failureNotice = '응답을 받지 못했어요.';

  @override
  void initState() {
    super.initState();
    _messages.addAll(widget.initialMessages);
    _replyDetails.addAll(widget.initialReplies);
    _audio = widget.audioPlayer ?? DeviceReplyAudioPlayer();
    _audioComplete = _audio.completed.listen((_) {
      if (mounted) setState(() => _playing = null);
    });
    _audioErrors = _audio.errors.listen((_) => _audioFailed());
    WidgetsBinding.instance.addObserver(this);
    if (_messages.isNotEmpty) _scrollToLatest();
  }

  String? _latestText({required bool isUser}) {
    for (final message in _messages.reversed) {
      if (message.isUser == isUser) return message.text;
    }
    return null;
  }

  ChatReply? _lastReplyDetails() {
    for (var index = _messages.length - 1; index >= 0; index--) {
      if (!_messages[index].isUser) return _replyDetails[index];
    }
    return null;
  }

  SpeechAudio? _lastReplyAudio() {
    for (var index = _messages.length - 1; index >= 0; index--) {
      if (!_messages[index].isUser) return _speechCache[index];
    }
    return null;
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _speechGeneration++;
    unawaited(_audioComplete.cancel());
    unawaited(_audioErrors.cancel());
    unawaited(_audio.dispose().catchError((Object _) {}));
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) unawaited(_stopAudio());
  }

  Future<void> _stopAudio() async {
    _speechGeneration++;
    if (mounted) {
      setState(() {
        _speechBusy = null;
        _playing = null;
      });
    }
    try {
      await _audio.stop();
    } catch (_) {}
  }

  void _audioFailed() {
    if (!mounted) return;
    final index = _speechBusy ?? _playing;
    if (index != null) setState(() => _speechFailed.add(index));
    unawaited(_stopAudio());
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
    if (widget.blockedReason != null ||
        _loading ||
        text.isEmpty ||
        text.length > 2000 ||
        (_failedMessage != null && retry == null)) {
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
      final response =
          await (() async => widget.service is DetailedChatService
                  ? await (widget.service as DetailedChatService).replyDetailed(
                      text,
                    )
                  : ChatReply(text: await widget.service.reply(text)))()
              .timeout(const Duration(seconds: 30));
      if (!mounted) return;
      if (response.text.trim().isEmpty) throw StateError('Empty response');
      setState(() {
        _replyDetails[_messages.length] = response;
        _messages.add((text: response.text, isUser: false));
      });
    } on BackendFailure catch (failure) {
      if (!mounted ||
          failure.kind == BackendFailureKind.unauthenticated ||
          failure.kind == BackendFailureKind.staleSession) {
        return;
      }
      setState(() {
        _failedMessage = text;
        _retryable = failure.code == null
            ? failure.kind != BackendFailureKind.forbidden
            : failure.retryable;
        _failureNotice = switch (failure.code) {
          'AI_BUSY' => '지금 다른 요청을 처리하고 있어요. 잠시 후 다시 시도해 주세요.',
          'AI_NOT_READY' => 'AI를 준비하고 있어요. 잠시 후 다시 시도해 주세요.',
          'AI_TIMEOUT' => '답변을 만드는 데 시간이 오래 걸리고 있어요. 잠시 후 다시 시도해 주세요.',
          'AI_UNAVAILABLE' => 'AI 서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.',
          'MESSAGE_TOO_LONG' => '질문이 너무 길어요. 더 짧게 입력해 주세요.',
          'CONFLICT' => '이미 처리 중이거나 상태가 바뀌었어요. 이력에서 새로고침 후 확인해 주세요.',
          _ =>
            failure.kind == BackendFailureKind.forbidden
                ? '이 대화에 접근할 수 없어요.'
                : '응답을 받지 못했어요.',
        };
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _failedMessage = text;
        _retryable = true;
        _failureNotice = '응답을 받지 못했어요.';
      });
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

  Future<void> _listen(int index) async {
    if (_speechPrompt || _speechBusy != null) return;
    if (_playing == index) {
      await _stopAudio();
      return;
    }
    final reply = _replyDetails[index];
    if (widget.service.isDemo ||
        widget.service is! DetailedChatService ||
        reply?.turnId == null) {
      _speechPrompt = true;
      try {
        await showDialog<void>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('합성한 목소리예요'),
            content: const Text(
              'AI 응답을 합성 음성으로 읽는 기능입니다. 현재 이 대화에서는 음성 재생을 제공하지 않습니다.',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('확인'),
              ),
            ],
          ),
        );
      } finally {
        _speechPrompt = false;
      }
      return;
    }
    _speechPrompt = true;
    final generationBeforePrompt = _speechGeneration;
    try {
      if (!_voiceAcknowledged) {
        final accepted = await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('합성한 목소리예요'),
            content: const Text('이 답변은 AI가 합성한 음성으로 재생돼요. 실제 인물이 말한 녹음이 아니에요.'),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: const Text('취소'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('재생'),
              ),
            ],
          ),
        );
        if (accepted != true ||
            !mounted ||
            generationBeforePrompt != _speechGeneration) {
          return;
        }
        _voiceAcknowledged = true;
      }
      if (!mounted) return;
      final generation = ++_speechGeneration;
      setState(() {
        _speechBusy = index;
        _playing = null;
        _speechFailed.remove(index);
      });
      try {
        await _audio.stop();
        final audio =
            _speechCache[index] ??
            await (widget.service as DetailedChatService)
                .synthesize(reply!)
                .timeout(const Duration(seconds: 30));
        if (!mounted || generation != _speechGeneration) return;
        _speechCache[index] = audio;
        await _audio.play(audio);
        if (!mounted || generation != _speechGeneration) return;
        setState(() {
          _speechBusy = null;
          _playing = index;
        });
      } on BackendFailure catch (failure) {
        if (!mounted || generation != _speechGeneration) return;
        if (failure.kind == BackendFailureKind.unauthenticated ||
            failure.kind == BackendFailureKind.staleSession) {
          return;
        }
        _audioFailed();
      } catch (_) {
        if (mounted && generation == _speechGeneration) _audioFailed();
      } finally {
        if (mounted && generation == _speechGeneration) {
          setState(() => _speechBusy = null);
        }
      }
    } finally {
      _speechPrompt = false;
    }
  }

  Widget _bubble(
    String text, {
    required bool isUser,
    Widget? footer,
    bool noEvidence = false,
  }) {
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: FractionallySizedBox(
        widthFactor: .88,
        child: Column(
          crossAxisAlignment: isUser
              ? CrossAxisAlignment.end
              : CrossAxisAlignment.start,
          children: [
            CustomPaint(
              foregroundPainter: noEvidence ? const NoEvidenceBorder() : null,
              child: Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 13,
                ),
                decoration: BoxDecoration(
                  color: isUser
                      ? _green
                      : noEvidence
                      ? const Color(0xFFECEFED)
                      : Colors.white,
                  border: isUser || noEvidence
                      ? null
                      : Border.all(color: _border),
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
            ),
            if (footer != null)
              Padding(padding: const EdgeInsets.only(top: 6), child: footer),
          ],
        ),
      ),
    );
  }

  Widget _aiActions(int index) {
    final reply = _replyDetails[index];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 4,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            const AiBadge(),
            if (reply?.evidence == EvidenceState.available &&
                reply!.sources.isNotEmpty)
              OutlinedButton.icon(
                onPressed: () => showReplyEvidence(context, reply),
                icon: const Icon(Icons.source_outlined, size: 17),
                label: Text('근거 기록 ${reply.sources.length}'),
              ),
            if (reply?.evidence == EvidenceState.none)
              const Text(
                '근거 기록 없음',
                style: TextStyle(fontSize: 12, color: Color(0xFF68766F)),
              ),
            if (reply == null || reply.evidence == EvidenceState.unknown)
              const Text(
                '근거 정보 미제공',
                style: TextStyle(fontSize: 12, color: Color(0xFF68766F)),
              ),
            OutlinedButton.icon(
              onPressed: _speechBusy != null ? null : () => _listen(index),
              style: OutlinedButton.styleFrom(
                foregroundColor: const Color(0xFF57645D),
                backgroundColor: Colors.white,
                side: const BorderSide(color: _border),
                minimumSize: const Size(0, 40),
                padding: const EdgeInsets.symmetric(horizontal: 12),
              ),
              icon: _speechBusy == index
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : Icon(
                      _playing == index ? Icons.stop : Icons.volume_up_outlined,
                      size: 17,
                    ),
              label: Text(
                _speechBusy == index
                    ? '음성 준비 중'
                    : _playing == index
                    ? '재생 중지'
                    : _speechFailed.contains(index)
                    ? '듣기 다시 시도'
                    : '듣기',
              ),
            ),
          ],
        ),
        if (reply?.finishReason == 'length')
          const Padding(
            padding: EdgeInsets.only(top: 6),
            child: Text(
              '답변이 길이 제한으로 일부 생략되었어요.',
              style: TextStyle(fontSize: 12, color: Color(0xFF68766F)),
            ),
          ),
        if (_speechFailed.contains(index))
          const Padding(
            padding: EdgeInsets.only(top: 6),
            child: Text(
              '음성을 재생하지 못했어요. 답변 글은 그대로 볼 수 있어요.',
              style: TextStyle(fontSize: 12, color: Color(0xFF9C5818)),
            ),
          ),
        if (_playing == index)
          const Padding(
            padding: EdgeInsets.only(top: 6),
            child: Text(
              'AI 합성 음성 재생 중',
              style: TextStyle(fontSize: 12, color: Color(0xFF68766F)),
            ),
          ),
      ],
    );
  }

  Widget _failureActions() {
    return Wrap(
      spacing: 4,
      children: [
        if (_retryable)
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
    if (widget.initialVoiceMode) {
      return VoiceScreen(
        service: widget.service,
        recorder: widget.voiceRecorder,
        audioPlayer: widget.voiceAudioPlayer,
        blockedReason: widget.blockedReason,
        conversationName: widget.conversationName,
        lastQuestion: _latestText(isUser: true),
        lastReply: _latestText(isUser: false),
        lastReplyDetails: _lastReplyDetails(),
        initialAudio: _lastReplyAudio(),
        onAudio: (reply, audio) {
          if (!mounted) return;
          for (final entry in _replyDetails.entries) {
            if (entry.value.turnId == reply.turnId) {
              _speechCache[entry.key] = audio;
            }
          }
        },
        onReply: (result) {
          if (!mounted) return;
          setState(() {
            _messages.add((text: result.transcript, isUser: true));
            final index = _messages.length;
            _replyDetails[index] = result.reply;
            if (result.audio != null) _speechCache[index] = result.audio!;
            _messages.add((text: result.reply.text, isUser: false));
          });
        },
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
            onPressed: () =>
                showAiDisclosure(context, isDemo: widget.service.isDemo),
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
                    keyboardDismissBehavior:
                        ScrollViewKeyboardDismissBehavior.onDrag,
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
                            noEvidence:
                                _replyDetails[index]?.evidence ==
                                EvidenceState.none,
                            footer: !_messages[index].isUser
                                ? _aiActions(index)
                                : _failedMessage != null &&
                                      _retryable &&
                                      index == _messages.length - 1
                                ? IconButton(
                                    tooltip: '다시 보내기',
                                    onPressed: () =>
                                        _send(retry: _failedMessage),
                                    icon: const Icon(
                                      Icons.refresh,
                                      color: _green,
                                    ),
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
                            _failureNotice,
                            isUser: false,
                            footer: _failureActions(),
                          ),
                        ),
                    ],
                  ),
                ),
                if (widget.blockedReason != null)
                  Padding(
                    padding: const EdgeInsets.all(12),
                    child: Text(widget.blockedReason!),
                  ),
                if (widget.historyNotice != null)
                  Padding(
                    padding: const EdgeInsets.all(12),
                    child: Text(widget.historyNotice!),
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
                      Expanded(
                        child: TextField(
                          controller: _input,
                          onTapOutside: (_) => FocusScope.of(context).unfocus(),
                          readOnly:
                              widget.blockedReason != null ||
                              _loading ||
                              _failedMessage != null,
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
                                  widget.blockedReason != null ||
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
