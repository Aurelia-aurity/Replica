import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../services/backend_service.dart';
import '../shared/conversation_widgets.dart';
import 'chat_service.dart';
import 'reply_audio_player.dart';
import 'reply_evidence.dart';
import 'voice_recorder.dart';

enum VoicePhase { idle, recording, processing, ready, failed }

class VoiceScreen extends StatefulWidget {
  const VoiceScreen({
    super.key,
    required this.conversationName,
    required this.service,
    this.onReply,
    this.onAudio,
    this.lastQuestion,
    this.lastReply,
    this.lastReplyDetails,
    this.initialAudio,
    this.blockedReason,
    this.recorder,
    this.audioPlayer,
  });
  final String conversationName;
  final ChatService service;
  final ValueChanged<VoiceReply>? onReply;
  final void Function(ChatReply reply, SpeechAudio audio)? onAudio;
  final String? lastQuestion, lastReply, blockedReason;
  final ChatReply? lastReplyDetails;
  final SpeechAudio? initialAudio;
  final VoiceRecorder? recorder;
  final ReplyAudioPlayer? audioPlayer;
  @override
  State<VoiceScreen> createState() => _VoiceScreenState();
}

class _VoiceScreenState extends State<VoiceScreen> with WidgetsBindingObserver {
  static const _surface = Color(0xFF17231D),
      _card = Color(0xFF24352C),
      _muted = Color(0xFFB2C8BB);
  late final VoiceRecorder _recorder;
  late final ReplyAudioPlayer _player;
  final _subscriptions = <StreamSubscription<dynamic>>[];
  final _levels = <double>[];
  VoicePhase _phase = VoicePhase.idle;
  Timer? _timer;
  Duration _elapsed = Duration.zero,
      _position = Duration.zero,
      _duration = Duration.zero;
  VoiceRecording? _pending;
  String? _question, _error, _failedStage;
  ChatReply? _reply;
  SpeechAudio? _audio;
  bool _starting = false,
      _ttsBusy = false,
      _permission = false,
      _retryable = false;
  bool _playing = false,
      _paused = false,
      _acknowledged = false,
      _prompt = false,
      _suspended = false;
  bool _ttsFailed = false;
  int _captureGeneration = 0, _playGeneration = 0;
  bool get _busy =>
      _starting || _ttsBusy || _phase == VoicePhase.processing || _prompt;
  bool get _recording => _phase == VoicePhase.recording;
  bool get _preview => widget.service.isDemo;

  @override
  void initState() {
    super.initState();
    _recorder =
        widget.recorder ??
        (_preview ? PreviewVoiceRecorder() : DeviceVoiceRecorder());
    _player = widget.audioPlayer ?? DeviceReplyAudioPlayer();
    _question = widget.lastQuestion;
    _reply =
        widget.lastReplyDetails ??
        (widget.lastReply == null ? null : ChatReply(text: widget.lastReply!));
    _audio = widget.initialAudio;
    _subscriptions.add(
      _recorder.levels.listen((value) {
        if (mounted && _recording) {
          setState(() {
            _levels.add(value.clamp(0, 1));
            if (_levels.length > 48) _levels.removeAt(0);
          });
        }
      }),
    );
    _subscriptions.add(
      _recorder.errors.listen((error) {
        if (mounted && _recording) unawaited(_captureFailed(error));
      }),
    );
    _subscriptions.add(
      _player.completed.listen((_) {
        if (mounted) {
          setState(() {
            _playing = false;
            _paused = false;
            _position = _duration;
          });
        }
      }),
    );
    _subscriptions.add(
      _player.errors.listen((_) {
        if (mounted) _playbackFailed();
      }),
    );
    if (_player is InteractiveReplyAudioPlayer) {
      final player = _player;
      _subscriptions.add(
        player.position.listen((value) {
          if (mounted) setState(() => _position = value);
        }),
      );
      _subscriptions.add(
        player.duration.listen((value) {
          if (mounted) setState(() => _duration = value);
        }),
      );
    }
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    _captureGeneration++;
    _playGeneration++;
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    for (final subscription in _subscriptions) {
      unawaited(subscription.cancel());
    }
    unawaited(_recorder.dispose().catchError((Object _) {}));
    unawaited(_player.dispose().catchError((Object _) {}));
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _suspended = state != AppLifecycleState.resumed;
    if (_suspended) {
      unawaited(_stopPlayback());
      if (_recording) unawaited(_cancelRecording());
    }
  }

  Future<void> _stopPlayback() async {
    _playGeneration++;
    if (mounted) {
      setState(() {
        _playing = false;
        _paused = false;
        _ttsBusy = false;
      });
    }
    try {
      await _player.stop();
    } catch (_) {}
  }

  Future<void> _begin() async {
    if (_busy ||
        _recording ||
        widget.blockedReason != null ||
        widget.service is! VoiceChatService) {
      return;
    }
    final generation = ++_captureGeneration;
    setState(() {
      _starting = true;
      _error = null;
      _permission = false;
      _failedStage = null;
    });
    try {
      await _stopPlayback();
      await _recorder.start();
      if (!mounted || generation != _captureGeneration || _suspended) {
        await _recorder.cancel();
        return;
      }
      setState(() {
        _phase = VoicePhase.recording;
        _pending = null;
        _elapsed = Duration.zero;
        _levels.clear();
      });
      _timer = Timer.periodic(const Duration(milliseconds: 100), (_) {
        if (!mounted || !_recording) return;
        setState(() => _elapsed += const Duration(milliseconds: 100));
        if (_elapsed >= const Duration(seconds: 60)) unawaited(_finish());
      });
    } catch (error) {
      if (mounted && generation == _captureGeneration) {
        await _captureFailed(error);
      }
    } finally {
      if (mounted && generation == _captureGeneration) {
        setState(() => _starting = false);
      }
    }
  }

  Future<void> _captureFailed(Object error) async {
    _timer?.cancel();
    try {
      await _recorder.cancel();
    } catch (_) {}
    if (!mounted) return;
    setState(() {
      _phase = VoicePhase.failed;
      _starting = false;
      _pending = null;
      _permission = error == CaptureFailure.permission;
      _failedStage = 'recording';
      _error = switch (error) {
        CaptureFailure.permission => '음성 질문을 녹음하려면 마이크 권한이 필요해요. 설정에서 허용해 주세요.',
        CaptureFailure.empty => '잘 들리지 않았어요. 다시 말씀해 주세요.',
        CaptureFailure.tooLarge => '녹음이 너무 커요. 25MB 이내로 다시 녹음해 주세요.',
        _ => '녹음을 시작하거나 읽지 못했어요. 마이크와 브라우저의 보안 연결을 확인해 주세요.',
      };
    });
  }

  Future<void> _cancelRecording() async {
    _captureGeneration++;
    _timer?.cancel();
    if (mounted) {
      setState(() {
        _phase = VoicePhase.idle;
        _starting = false;
        _pending = null;
      });
    }
    try {
      await _recorder.cancel();
    } catch (_) {}
  }

  Future<void> _finish() async {
    if (!_recording || _starting) return;
    _timer?.cancel();
    setState(() => _phase = VoicePhase.processing);
    try {
      final recording = await _recorder.stop();
      if (!mounted) return;
      recording.validate();
      _pending = recording;
      await _send(recording);
    } catch (error) {
      if (mounted) await _captureFailed(error);
    }
  }

  Future<void> _send(VoiceRecording recording) async {
    setState(() {
      _phase = VoicePhase.processing;
      _error = null;
      _failedStage = null;
      _ttsFailed = false;
    });
    try {
      final result = await (widget.service as VoiceChatService)
          .replyVoice(recording)
          .timeout(const Duration(seconds: 30));
      if (!mounted) return;
      widget.onReply?.call(result);
      setState(() {
        _question = result.transcript;
        _reply = result.reply;
        _audio = result.audio;
        _phase = VoicePhase.ready;
        _ttsFailed = result.ttsFailed;
        _failedStage = result.ttsFailed ? 'tts' : null;
        _pending = null;
        _position = Duration.zero;
        _duration = Duration.zero;
      });
    } on BackendFailure catch (failure) {
      if (!mounted ||
          failure.kind == BackendFailureKind.unauthenticated ||
          failure.kind == BackendFailureKind.staleSession) {
        return;
      }
      setState(() {
        _phase = VoicePhase.failed;
        _failedStage = failure.stage;
        _retryable =
            failure.retryable || failure.kind == BackendFailureKind.network;
        _error = switch (failure.code) {
          'EMPTY_TRANSCRIPT' || 'STT_FAILED' => '잘 들리지 않았어요. 다시 말씀해 주세요.',
          'AI_BUSY' => '지금 다른 요청을 처리하고 있어요. 잠시 후 다시 시도해 주세요.',
          'AI_NOT_READY' => 'AI를 준비하고 있어요. 잠시 후 다시 시도해 주세요.',
          'FILE_TOO_LARGE' => '녹음이 너무 커요. 25MB 이내로 다시 녹음해 주세요.',
          'CONFLICT' => '이미 처리 중이거나 상태가 바뀌었어요. 이력에서 새로고침해 주세요.',
          _ => '응답을 받지 못했어요. 연결 상태를 확인하고 다시 시도해 주세요.',
        };
      });
    } catch (_) {
      if (mounted) {
        setState(() {
          _phase = VoicePhase.failed;
          _retryable = true;
          _error = '응답을 받지 못했어요. 다시 시도해 주세요.';
        });
      }
    }
  }

  void _playbackFailed() {
    if (!mounted) return;
    setState(() {
      _ttsFailed = true;
      _failedStage = 'tts';
      _ttsBusy = false;
    });
    unawaited(_stopPlayback());
  }

  Future<void> _play() async {
    if (_busy || _recording || _reply == null) return;
    if (_playing) {
      if (_player is InteractiveReplyAudioPlayer) {
        try {
          await (_player).pause();
          if (mounted) {
            setState(() {
              _playing = false;
              _paused = true;
            });
          }
        } catch (_) {
          _playbackFailed();
        }
      } else {
        await _stopPlayback();
      }
      return;
    }
    _prompt = true;
    final generation = ++_playGeneration;
    try {
      if (_preview || !_acknowledged) {
        final accepted = await showDialog<bool>(
          context: context,
          builder: (context) => AlertDialog(
            title: const Text('합성한 목소리예요'),
            content: Text(
              _preview
                  ? '미리보기에서는 음성을 합성하거나 재생하지 않습니다.'
                  : '이어지는 목소리는 AI가 합성한 음성이에요. 실제 인물이 말한 녹음이 아니에요.',
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context, false),
                child: Text(_preview ? '확인' : '취소'),
              ),
              if (!_preview)
                FilledButton(
                  onPressed: () => Navigator.pop(context, true),
                  child: const Text('재생'),
                ),
            ],
          ),
        );
        if (_preview ||
            accepted != true ||
            !mounted ||
            generation != _playGeneration ||
            _suspended) {
          return;
        }
        _acknowledged = true;
      }
      if (!mounted || generation != _playGeneration || _suspended) return;
      setState(() => _ttsBusy = true);
      if (_audio == null) {
        if (widget.service is! DetailedChatService || _reply!.turnId == null) {
          throw StateError('No speech service');
        }
        final audio = await (widget.service as DetailedChatService)
            .synthesize(_reply!)
            .timeout(const Duration(seconds: 30));
        if (!mounted || generation != _playGeneration || _suspended) return;
        _audio = audio;
        widget.onAudio?.call(_reply!, audio);
      }
      if (_paused && _player is InteractiveReplyAudioPlayer) {
        await (_player).resume();
      } else {
        await _player.play(_audio!);
      }
      if (!mounted || generation != _playGeneration || _suspended) return;
      setState(() {
        _playing = true;
        _paused = false;
        _ttsFailed = false;
        _failedStage = null;
      });
    } on BackendFailure catch (failure) {
      if (mounted &&
          generation == _playGeneration &&
          failure.kind != BackendFailureKind.unauthenticated &&
          failure.kind != BackendFailureKind.staleSession) {
        _playbackFailed();
      }
    } catch (_) {
      if (mounted && generation == _playGeneration) _playbackFailed();
    } finally {
      _prompt = false;
      if (mounted && generation == _playGeneration) {
        setState(() => _ttsBusy = false);
      }
    }
  }

  Future<void> _settings() async {
    var opened = false;
    try {
      opened = await _recorder.openSettings();
    } catch (_) {}
    if (!opened && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('기기 설정 또는 브라우저의 사이트 설정에서 마이크 권한을 허용해 주세요.'),
        ),
      );
    }
  }

  String _time(Duration value) =>
      '${value.inMinutes}:${(value.inSeconds % 60).toString().padLeft(2, '0')}';
  int _stageState(int index) {
    final failed = switch (_failedStage) {
      'recording' => 0,
      'stt' => 1,
      'llm' => 2,
      'tts' => 3,
      _ => -1,
    };
    if (failed == index) return 3;
    if (_recording) return index == 0 ? 1 : 0;
    if (_phase == VoicePhase.processing) return index == 0 ? 2 : 1;
    if (_phase == VoicePhase.ready) return index == 3 && _preview ? 0 : 2;
    if (_phase == VoicePhase.failed && failed >= 0) {
      return index < failed ? 2 : 0;
    }
    return 0;
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: _surface,
    appBar: AppBar(
      backgroundColor: _surface,
      foregroundColor: Colors.white,
      surfaceTintColor: Colors.transparent,
      title: Text(widget.conversationName),
      actions: const [
        Padding(
          padding: EdgeInsets.only(right: 16),
          child: Center(child: AiBadge(label: 'AI 생성 대화')),
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
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
                child: Row(
                  children: [
                    for (var index = 0; index < 4; index++)
                      Expanded(
                        child: Column(
                          children: [
                            Icon(
                              switch (_stageState(index)) {
                                1 => Icons.pending_outlined,
                                2 => Icons.check_circle_outline,
                                3 => Icons.error_outline,
                                _ => Icons.circle_outlined,
                              },
                              size: 17,
                              color: _stageState(index) == 3
                                  ? const Color(0xFFFFB4AB)
                                  : _muted,
                            ),
                            const SizedBox(height: 6),
                            Text(
                              ['녹음', '인식', '응답 생성', '음성 합성'][index],
                              style: const TextStyle(
                                color: _muted,
                                fontSize: 11,
                              ),
                            ),
                            if (_phase == VoicePhase.ready &&
                                _reply?.timings[[
                                      '',
                                      'stt_ms',
                                      'llm_ms',
                                      'tts_ms',
                                    ][index]] !=
                                    null)
                              Text(
                                '${_reply!.timings[['', 'stt_ms', 'llm_ms', 'tts_ms'][index]]}ms',
                                style: const TextStyle(
                                  color: _muted,
                                  fontSize: 10,
                                ),
                              ),
                          ],
                        ),
                      ),
                  ],
                ),
              ),
              Expanded(
                child: ListView(
                  padding: const EdgeInsets.symmetric(horizontal: 20),
                  children: [
                    if (_preview)
                      const Text(
                        '음성 화면 미리보기 · 마이크는 사용하지 않습니다.',
                        style: TextStyle(color: _muted, fontSize: 12),
                        textAlign: TextAlign.center,
                      ),
                    if (_phase == VoicePhase.processing)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 16),
                        child: Semantics(
                          liveRegion: true,
                          child: const Text(
                            '인식·응답 생성·음성 합성을 처리 중이에요.',
                            style: TextStyle(color: _muted),
                          ),
                        ),
                      ),
                    const SizedBox(height: 20),
                    const Text(
                      '내 질문 · 음성 인식',
                      textAlign: TextAlign.right,
                      style: TextStyle(color: _muted, fontSize: 12),
                    ),
                    const SizedBox(height: 8),
                    Align(
                      alignment: Alignment.centerRight,
                      child: Container(
                        padding: const EdgeInsets.all(16),
                        decoration: BoxDecoration(
                          color: replicaGreen,
                          borderRadius: BorderRadius.circular(20),
                        ),
                        child: Text(
                          _question ?? '아직 인식된 질문이 없어요. 눌러서 말해 보세요.',
                          style: const TextStyle(
                            color: Colors.white,
                            height: 1.6,
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(height: 20),
                    Container(
                      padding: const EdgeInsets.all(16),
                      decoration: BoxDecoration(
                        color: aiAmberBackground,
                        borderRadius: BorderRadius.circular(16),
                      ),
                      child: const Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Icon(Icons.info_outline, color: aiAmber, size: 18),
                          SizedBox(width: 8),
                          Expanded(
                            child: Text(
                              '이어지는 목소리는 기록을 바탕으로 AI가 합성한 음성이에요.',
                              style: TextStyle(
                                color: aiAmber,
                                height: 1.5,
                                fontSize: 13,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 16),
                    if (_error != null)
                      Container(
                        padding: const EdgeInsets.all(16),
                        margin: const EdgeInsets.only(bottom: 16),
                        decoration: BoxDecoration(
                          color: const Color(0xFF4A2927),
                          borderRadius: BorderRadius.circular(16),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Semantics(
                              liveRegion: true,
                              child: Text(
                                _error!,
                                style: const TextStyle(
                                  color: Color(0xFFFFDAD6),
                                  height: 1.5,
                                ),
                              ),
                            ),
                            if (_permission)
                              TextButton(
                                onPressed: _settings,
                                child: const Text(
                                  '설정 열기',
                                  style: TextStyle(color: Colors.white),
                                ),
                              ),
                            if (_pending != null && _retryable)
                              TextButton(
                                onPressed: _busy
                                    ? null
                                    : () => _send(_pending!),
                                child: const Text(
                                  '같은 녹음 다시 전송',
                                  style: TextStyle(color: Colors.white),
                                ),
                              ),
                          ],
                        ),
                      ),
                    Container(
                      padding: const EdgeInsets.all(18),
                      decoration: BoxDecoration(
                        color: _card,
                        borderRadius: BorderRadius.circular(20),
                      ),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(
                            children: [
                              IconButton(
                                tooltip: _playing ? '음성 일시정지' : '답변 음성 재생',
                                onPressed: _reply == null || _busy || _recording
                                    ? null
                                    : _play,
                                icon: Icon(
                                  _playing ? Icons.pause : Icons.play_arrow,
                                  color: _reply == null ? _muted : Colors.white,
                                ),
                              ),
                              Expanded(
                                child: Text(
                                  _ttsBusy
                                      ? '음성 준비 중'
                                      : _playing
                                      ? '합성 음성 재생 중'
                                      : _paused
                                      ? '일시정지'
                                      : '합성 음성 듣기',
                                  style: const TextStyle(color: _muted),
                                ),
                              ),
                              Text(
                                _time(_position),
                                style: const TextStyle(
                                  color: _muted,
                                  fontSize: 12,
                                ),
                              ),
                            ],
                          ),
                          SizedBox(
                            height: 40,
                            width: double.infinity,
                            child: CustomPaint(
                              painter: VoiceWaveform(
                                _recording ? _levels : _waveform(_audio),
                                _duration.inMilliseconds > 0
                                    ? (_position.inMilliseconds /
                                              _duration.inMilliseconds)
                                          .clamp(0, 1)
                                    : 0,
                              ),
                            ),
                          ),
                          if (_duration.inMilliseconds > 0)
                            Slider(
                              value:
                                  (_position.inMilliseconds /
                                          _duration.inMilliseconds)
                                      .clamp(0, 1),
                              semanticFormatterCallback: (value) => _time(
                                Duration(
                                  milliseconds:
                                      (_duration.inMilliseconds * value)
                                          .round(),
                                ),
                              ),
                              onChanged:
                                  _player is InteractiveReplyAudioPlayer &&
                                      !_recording &&
                                      !_busy
                                  ? (value) async {
                                      try {
                                        await (_player).seek(
                                          Duration(
                                            milliseconds:
                                                (_duration.inMilliseconds *
                                                        value)
                                                    .round(),
                                          ),
                                        );
                                      } catch (_) {
                                        _playbackFailed();
                                      }
                                    }
                                  : null,
                            ),
                          const SizedBox(height: 12),
                          Text(
                            _reply?.text ?? '답변이 도착하면 이곳에 표시돼요.',
                            style: const TextStyle(
                              color: Colors.white,
                              height: 1.6,
                            ),
                          ),
                          if (_reply != null)
                            Padding(
                              padding: const EdgeInsets.only(top: 16),
                              child: Wrap(
                                spacing: 8,
                                runSpacing: 8,
                                children: [
                                  const AiBadge(),
                                  if (_reply!.sources.isNotEmpty)
                                    OutlinedButton(
                                      onPressed: () =>
                                          showReplyEvidence(context, _reply!),
                                      child: Text(
                                        '근거 기록 ${_reply!.sources.length}',
                                        style: const TextStyle(
                                          color: Colors.white,
                                        ),
                                      ),
                                    ),
                                  if (_reply!.evidence == EvidenceState.none)
                                    const Text(
                                      '근거 기록 없음',
                                      style: TextStyle(color: _muted),
                                    ),
                                ],
                              ),
                            ),
                          if (_ttsFailed)
                            Padding(
                              padding: const EdgeInsets.only(top: 12),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  const Text(
                                    '음성 합성·재생에 실패했어요. 답변 글은 그대로 볼 수 있어요.',
                                    style: TextStyle(
                                      color: Color(0xFFFFDAD6),
                                      height: 1.5,
                                    ),
                                  ),
                                  TextButton(
                                    onPressed: _busy || _recording
                                        ? null
                                        : _play,
                                    child: const Text(
                                      '음성 다시 시도',
                                      style: TextStyle(color: Colors.white),
                                    ),
                                  ),
                                ],
                              ),
                            ),
                        ],
                      ),
                    ),
                    if (widget.blockedReason != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 16),
                        child: Text(
                          widget.blockedReason!,
                          style: const TextStyle(color: _muted),
                        ),
                      ),
                    const SizedBox(height: 20),
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 12, 20, 16),
                child: Row(
                  children: [
                    const SizedBox(width: 48),
                    Expanded(
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (_recording)
                            Text(
                              _time(_elapsed),
                              style: const TextStyle(
                                color: Colors.white,
                                fontSize: 18,
                              ),
                            ),
                          SizedBox(
                            width: 76,
                            height: 76,
                            child: IconButton.filled(
                              tooltip: _recording ? '녹음 종료 후 전송' : '눌러서 말하기',
                              onPressed:
                                  _busy ||
                                      widget.blockedReason != null ||
                                      widget.service is! VoiceChatService
                                  ? null
                                  : _recording
                                  ? _finish
                                  : _begin,
                              style: IconButton.styleFrom(
                                backgroundColor: _recording
                                    ? const Color(0xFFC14B45)
                                    : const Color(0xFF84C5AD),
                                foregroundColor: _surface,
                              ),
                              icon: _busy
                                  ? const SizedBox(
                                      width: 26,
                                      height: 26,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                      ),
                                    )
                                  : Icon(
                                      _recording ? Icons.stop : Icons.mic_none,
                                      size: 34,
                                    ),
                            ),
                          ),
                          const SizedBox(height: 8),
                          Text(
                            _recording
                                ? '탭하면 전송 · 최대 1분'
                                : _preview
                                ? '말하기 흐름 미리보기'
                                : '눌러서 말하기',
                            style: const TextStyle(color: _muted, fontSize: 12),
                          ),
                        ],
                      ),
                    ),
                    if (_recording)
                      IconButton(
                        tooltip: '녹음 취소',
                        onPressed: _cancelRecording,
                        icon: const Icon(Icons.close, color: Colors.white),
                      )
                    else
                      const SizedBox(width: 48),
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

List<double> _waveform(SpeechAudio? audio) {
  if (audio == null ||
      audio.mimeType != 'audio/wav' ||
      audio.bytes.length < 46) {
    return const [];
  }
  final data = ByteData.sublistView(audio.bytes);
  if (data.getUint16(20, Endian.little) != 1 ||
      data.getUint16(22, Endian.little) != 1 ||
      data.getUint16(34, Endian.little) != 16) {
    return const [];
  }
  var offset = 12;
  while (offset + 8 <= data.lengthInBytes) {
    final length = data.getUint32(offset + 4, Endian.little);
    if (audio.bytes[offset] == 100 &&
        audio.bytes[offset + 1] == 97 &&
        audio.bytes[offset + 2] == 116 &&
        audio.bytes[offset + 3] == 97) {
      final end = (offset + 8 + length).clamp(0, data.lengthInBytes);
      final count = (end - offset - 8) ~/ 2;
      if (count < 1) return const [];
      return List.generate(
        48,
        (i) =>
            data
                .getInt16(
                  offset + 8 + ((i * count ~/ 48).clamp(0, count - 1)) * 2,
                  Endian.little,
                )
                .abs() /
            32768,
      );
    }
    offset += 8 + length + length % 2;
  }
  return const [];
}

class VoiceWaveform extends CustomPainter {
  const VoiceWaveform(this.levels, this.progress);
  final List<double> levels;
  final double progress;
  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..strokeWidth = 3
      ..strokeCap = StrokeCap.round;
    if (levels.isEmpty) {
      paint.color = const Color(0xFF7B9385);
      canvas.drawLine(
        Offset(0, size.height / 2),
        Offset(size.width, size.height / 2),
        paint,
      );
      return;
    }
    for (var i = 0; i < levels.length; i++) {
      final x = (i + .5) * size.width / levels.length;
      final height = 3 + levels[i].clamp(0, 1) * (size.height - 3);
      paint.color = i / levels.length <= progress
          ? const Color(0xFF84C5AD)
          : const Color(0xFF7B9385);
      canvas.drawLine(
        Offset(x, (size.height - height) / 2),
        Offset(x, (size.height + height) / 2),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(VoiceWaveform oldDelegate) => true;
}
