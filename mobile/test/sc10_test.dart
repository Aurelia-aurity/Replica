import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/chat/chat_screen.dart';
import 'package:mobile/chat/chat_service.dart';
import 'package:mobile/chat/reply_audio_player.dart';
import 'package:mobile/chat/voice_recorder.dart';
import 'package:mobile/chat/voice_screen.dart';
import 'package:mobile/history/conversation_service.dart';
import 'package:mobile/services/backend_service.dart';

import 'history_test.dart' as fixtures;
import 'sc09_test.dart' as sc09;

VoiceRecording sampleRecording() => VoiceRecording(
  bytes: pcmToWave(Uint8List.fromList([0, 64, 0, 192])),
  duration: const Duration(seconds: 1),
);
VoiceReply sampleReply({bool ttsFailed = false}) => VoiceReply(
  transcript: '인식된 합성 질문',
  reply: const ChatReply(
    text: '음성 대화 합성 답변',
    turnId: 'voice-turn',
    timings: {'stt_ms': 15, 'llm_ms': 25, 'tts_ms': 10},
  ),
  audio: ttsFailed ? null : SpeechAudio(sampleRecording().bytes, 'audio/wav'),
  ttsFailed: ttsFailed,
);

class Sc10Recorder implements VoiceRecorder {
  final amplitudes = StreamController<double>.broadcast();
  final failures = StreamController<Object>.broadcast();
  int starts = 0, cancels = 0, stops = 0, settings = 0;
  bool disposed = false;
  Object? startFailure, stopFailure;
  Completer<void>? permissionPending;
  @override
  Stream<double> get levels => amplitudes.stream;
  @override
  Stream<Object> get errors => failures.stream;
  @override
  Future<void> start() async {
    starts++;
    if (startFailure != null) throw startFailure!;
    await permissionPending?.future;
  }

  @override
  Future<VoiceRecording> stop() async {
    stops++;
    if (stopFailure != null) throw stopFailure!;
    return sampleRecording();
  }

  @override
  Future<void> cancel() async {
    cancels++;
  }

  @override
  Future<bool> openSettings() async {
    settings++;
    return false;
  }

  @override
  Future<void> dispose() async {
    disposed = true;
    await amplitudes.close();
    await failures.close();
  }
}

class Sc10Service implements VoiceChatService {
  VoiceReply result = sampleReply();
  Object? failure;
  Completer<VoiceReply>? pending;
  int syntheses = 0;
  final recordings = <VoiceRecording>[];
  @override
  bool get isDemo => false;
  @override
  Future<String> reply(String message) async => '텍스트 합성 답변';
  @override
  Future<ChatReply> replyDetailed(String message) async =>
      const ChatReply(text: '텍스트 합성 답변');
  @override
  Future<SpeechAudio> synthesize(ChatReply reply) async {
    syntheses++;
    return SpeechAudio(sampleRecording().bytes, 'audio/wav');
  }

  @override
  Future<VoiceReply> replyVoice(VoiceRecording recording) async {
    recordings.add(recording);
    if (failure != null) throw failure!;
    return pending?.future ?? result;
  }
}

class Sc10Player extends sc09.Sc09Player
    implements InteractiveReplyAudioPlayer {
  final positions = StreamController<Duration>.broadcast();
  final durations = StreamController<Duration>.broadcast();
  int pauses = 0, resumes = 0;
  Duration? sought;
  @override
  Stream<Duration> get position => positions.stream;
  @override
  Stream<Duration> get duration => durations.stream;
  @override
  Future<void> pause() async {
    pauses++;
  }

  @override
  Future<void> resume() async {
    resumes++;
  }

  @override
  Future<void> seek(Duration value) async {
    sought = value;
    positions.add(value);
  }

  @override
  Future<void> dispose() async {
    await positions.close();
    await durations.close();
    await super.dispose();
  }
}

class ControlledVoiceConversation extends fixtures.TestHistory
    implements VoiceConversationService {
  final pending = <({String id, Completer<VoiceReply> result})>[];
  @override
  Future<VoiceReply> sendVoice(
    String id,
    String turnId,
    VoiceRecording recording,
  ) {
    final result = Completer<VoiceReply>();
    pending.add((id: turnId, result: result));
    return result.future;
  }

  @override
  Future<ChatReply> sendDetailed(
    String id,
    String turnId,
    String message,
  ) async => ChatReply(text: message, turnId: turnId);
  @override
  Future<SpeechAudio> synthesize(ChatReply reply) async =>
      SpeechAudio(sampleRecording().bytes, 'audio/wav');
}

Future<void> mountVoice(
  WidgetTester tester,
  Sc10Service service,
  Sc10Recorder recorder,
  Sc10Player player, {
  bool parent = false,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: parent
          ? ChatScreen(
              service: service,
              initialVoiceMode: true,
              audioPlayer: sc09.Sc09Player(),
              voiceRecorder: recorder,
              voiceAudioPlayer: player,
            )
          : VoiceScreen(
              conversationName: '합성 상대',
              service: service,
              recorder: recorder,
              audioPlayer: player,
            ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> startRecording(WidgetTester tester) async {
  await tester.tap(find.byTooltip('눌러서 말하기'));
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 400));
}

Future<void> finishRecording(
  WidgetTester tester, {
  bool pending = false,
}) async {
  await tester.tap(find.byTooltip('녹음 종료 후 전송'));
  await tester.pump();
  if (!pending) await tester.pumpAndSettle();
}

Future<void> acceptPlay(WidgetTester tester) async {
  await tester.ensureVisible(find.byTooltip('답변 음성 재생'));
  await tester.tap(find.byTooltip('답변 음성 재생'));
  await tester.pumpAndSettle();
  expect(find.text('합성한 목소리예요'), findsOneWidget);
  await tester.tap(find.widgetWithText(FilledButton, '재생'));
  await tester.pumpAndSettle();
}

void main() {
  test(
    'late completion of an older recording cannot replace a newer retry ID',
    () async {
      final service = ControlledVoiceConversation();
      final chat = ConversationChatService(service, 's');
      final first = chat.replyVoice(sampleRecording());
      final secondRecording = sampleRecording();
      final second = chat.replyVoice(secondRecording);
      service.pending[0].result.complete(sampleReply());
      await first;
      final check = expectLater(second, throwsA(isA<BackendFailure>()));
      service.pending[1].result.completeError(
        const BackendFailure(BackendFailureKind.network),
      );
      await check;
      final retry = chat.replyVoice(secondRecording);
      expect(service.pending[2].id, service.pending[1].id);
      service.pending[2].result.complete(sampleReply());
      await retry;
    },
  );
  testWidgets('one-minute recording limit stops and sends once', (
    tester,
  ) async {
    final service = Sc10Service();
    final recorder = Sc10Recorder();
    await mountVoice(tester, service, recorder, Sc10Player());
    await startRecording(tester);
    await tester.pump(const Duration(seconds: 60));
    await tester.pumpAndSettle();
    expect(recorder.stops, 1);
    expect(service.recordings.length, 1);
  });
  testWidgets(
    'leaving during microphone permission acquisition cancels late capture',
    (tester) async {
      final service = Sc10Service();
      final recorder = Sc10Recorder()..permissionPending = Completer<void>();
      await mountVoice(tester, service, recorder, Sc10Player());
      await tester.tap(find.byTooltip('눌러서 말하기'));
      await tester.pump();
      await tester.pumpWidget(const SizedBox());
      recorder.permissionPending!.complete();
      await tester.pumpAndSettle();
      expect(recorder.disposed, true);
      expect(recorder.cancels, greaterThan(0));
      expect(service.recordings, isEmpty);
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets(
    'late voice response after leaving does not update parent or play',
    (tester) async {
      final service = Sc10Service()..pending = Completer<VoiceReply>();
      final player = Sc10Player();
      var delivered = 0;
      await tester.pumpWidget(
        MaterialApp(
          home: VoiceScreen(
            conversationName: '합성 상대',
            service: service,
            recorder: Sc10Recorder(),
            audioPlayer: player,
            onReply: (_) => delivered++,
          ),
        ),
      );
      await tester.pumpAndSettle();
      await startRecording(tester);
      await finishRecording(tester, pending: true);
      await tester.pumpWidget(const SizedBox());
      service.pending!.complete(sampleReply());
      await tester.pumpAndSettle();
      expect(delivered, 0);
      expect(player.plays, 0);
      expect(player.disposed, true);
    },
  );
  test('late voice 401 cannot invalidate the next account', () async {
    final auth = fixtures.TestAuth();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    final pending = Completer<http.Response>();
    final client = MockClient((_) => pending.future);
    addTearDown(client.close);
    final service = HttpConversationService(
      backend: BackendService(auth),
      baseUrl: Uri.parse('http://localhost:8000'),
      client: client,
    );
    final check = expectLater(
      service.sendVoice('session-A', 'v', sampleRecording()),
      throwsA(isA<BackendFailure>()),
    );
    await Future<void>.delayed(Duration.zero);
    await auth.signOut();
    await auth.signIn(
      PreviewAuthService.emails.last,
      PreviewAuthService.password,
    );
    pending.complete(http.Response('unauthorized', 401));
    await check;
    expect(auth.identity!.email, PreviewAuthService.emails.last);
  });
  test('WAV header accurately describes PCM including hardware-adjusted rate and channels', () {
    final pcm = Uint8List.fromList([1, 0, 2, 0]);
    final wave = pcmToWave(pcm, sampleRate: 48000, channels: 2);
    final header = ByteData.sublistView(wave);
    expect(ascii.decode(wave.sublist(0, 4)), 'RIFF');
    expect(header.getUint32(4, Endian.little), 40);
    expect(header.getUint16(22, Endian.little), 2);
    expect(header.getUint32(24, Endian.little), 48000);
    expect(header.getUint32(28, Endian.little), 192000);
    expect(wave.sublist(44), pcm);
    expect(() => pcmToWave(Uint8List(1)), throwsFormatException);
  });

  test(
    'voice response validates transcript and keeps text on TTS partial failure',
    () {
      final json = {
        'turn_id': 'v',
        'transcript': ' 합성 질문 ',
        'answer': '합성 답변',
        'is_ai_generated': true,
        'finish_reason': 'stop',
        'tts_status': 'failed',
        'audio_base64': null,
        'audio_format': null,
      };
      final result = VoiceReply.fromJson(json, 'v');
      expect(result.transcript, '합성 질문');
      expect(result.reply.text, '합성 답변');
      expect(result.ttsFailed, true);
      final brokenAudio = VoiceReply.fromJson({
        ...json,
        'tts_status': 'ready',
        'audio_base64': '?',
        'audio_format': 'wav',
      }, 'v');
      expect(brokenAudio.reply.text, '합성 답변');
      expect(brokenAudio.ttsFailed, true);
      expect(
        () => VoiceReply.fromJson({...json, 'transcript': '  '}, 'v'),
        throwsA(isA<BackendFailure>()),
      );
      expect(
        () => VoiceReply.fromJson({...json, 'is_ai_generated': false}, 'v'),
        throwsFormatException,
      );
    },
  );

  test('HTTP voice multipart preserves session and retry ID, fresh audio creates a new turn', () async {
    final auth = fixtures.TestAuth();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    final ids = <String>[];
    var attempt = 0;
    final client = MockClient((request) async {
      expect(request.url.path, '/chat/voice');
      expect(request.method, 'POST');
      expect(request.headers['Authorization'], 'Bearer synthetic-token');
      expect(
        request.headers['content-type'],
        startsWith('multipart/form-data; boundary='),
      );
      final body = latin1.decode(request.bodyBytes);
      expect(body, contains('name="file"; filename="question.wav"'));
      expect(body.toLowerCase(), contains('content-type: audio/wav'));
      expect(body, contains('session-one'));
      expect(body, isNot(contains('user_id')));
      final id = RegExp('name="turn_id"\r\n\r\n([^\r]+)')
          .firstMatch(body)!
          .group(1)!;
      ids.add(id);
      attempt++;
      if (attempt == 1) {
        return fixtures.jsonResponse({
          'error': {'code': 'STT_FAILED', 'stage': 'stt', 'retryable': true},
        }, status: 502);
      }
      return fixtures.jsonResponse({
        'turn_id': id,
        'transcript': '합성 전사문',
        'answer': '합성 답변',
        'is_ai_generated': true,
        'finish_reason': 'stop',
        'tts_status': 'failed',
      });
    });
    addTearDown(client.close);
    final service = ConversationChatService(
      HttpConversationService(
        backend: BackendService(auth),
        baseUrl: Uri.parse('http://localhost:8000'),
        client: client,
      ),
      'session-one',
    );
    final recording = sampleRecording();
    await expectLater(
      service.replyVoice(recording),
      throwsA(
        isA<BackendFailure>().having((error) => error.stage, 'stage', 'stt'),
      ),
    );
    await service.replyVoice(recording);
    await service.replyVoice(sampleRecording());
    expect(ids[0], ids[1]);
    expect(ids[2], isNot(ids[1]));
  });

  test(
    'voice upload rejects empty and oversized bytes before dispatch',
    () async {
      final auth = fixtures.TestAuth();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      var dispatched = false;
      final client = MockClient((_) async {
        dispatched = true;
        return http.Response('', 200);
      });
      addTearDown(client.close);
      final service = HttpConversationService(
        backend: BackendService(auth),
        baseUrl: Uri.parse('http://localhost:8000'),
        client: client,
      );
      for (final bytes in [Uint8List(0), Uint8List(maxVoiceBytes + 1)]) {
        await expectLater(
          service.sendVoice(
            's',
            'v',
            VoiceRecording(bytes: bytes, duration: Duration.zero),
          ),
          throwsA(isA<BackendFailure>()),
        );
      }
      expect(dispatched, false);
    },
  );

  testWidgets(
    'record-stop displays transcript and stays in voice mode without a switch',
    (tester) async {
      final service = Sc10Service();
      final recorder = Sc10Recorder();
      final player = Sc10Player();
      await mountVoice(tester, service, recorder, player, parent: true);
      await startRecording(tester);
      recorder.amplitudes.add(.8);
      await tester.pump();
      expect(find.byTooltip('녹음 종료 후 전송'), findsOneWidget);
      await finishRecording(tester);
      expect(service.recordings.length, 1);
      expect(find.text('인식된 합성 질문'), findsOneWidget);
      expect(find.text('음성 대화 합성 답변'), findsOneWidget);
      expect(find.text('AI 생성'), findsOneWidget);
      expect(player.plays, 0);
      expect(find.byTooltip('텍스트 대화로 전환'), findsNothing);
      expect(find.byType(VoiceScreen), findsOneWidget);
      expect(find.byType(TextField), findsNothing);
      await tester.pumpWidget(const SizedBox());
      await tester.pump();
      expect(recorder.disposed, true);
    },
  );

  testWidgets(
    'permission failure explains need and offers app settings without sending',
    (tester) async {
      final service = Sc10Service();
      final recorder = Sc10Recorder()..startFailure = CaptureFailure.permission;
      await mountVoice(tester, service, recorder, Sc10Player());
      await tester.tap(find.byTooltip('눌러서 말하기'));
      await tester.pumpAndSettle();
      expect(find.textContaining('마이크 권한이 필요해요'), findsOneWidget);
      await tester.ensureVisible(find.text('설정 열기'));
      await tester.tap(find.text('설정 열기'));
      await tester.pumpAndSettle();
      expect(recorder.settings, 1);
      expect(service.recordings, isEmpty);
    },
  );

  testWidgets('silence is rejected without invoking backend', (tester) async {
    final service = Sc10Service();
    final recorder = Sc10Recorder()..stopFailure = CaptureFailure.empty;
    await mountVoice(tester, service, recorder, Sc10Player());
    await startRecording(tester);
    await finishRecording(tester);
    expect(find.text('잘 들리지 않았어요. 다시 말씀해 주세요.'), findsOneWidget);
    expect(service.recordings, isEmpty);
  });

  testWidgets('cancel and app background discard capture without uploading', (
    tester,
  ) async {
    final service = Sc10Service();
    final recorder = Sc10Recorder();
    await mountVoice(tester, service, recorder, Sc10Player());
    await startRecording(tester);
    await tester.tap(find.byTooltip('녹음 취소'));
    await tester.pumpAndSettle();
    expect(recorder.cancels, 1);
    expect(service.recordings, isEmpty);
    await startRecording(tester);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    await tester.pumpAndSettle();
    expect(recorder.cancels, 2);
    expect(service.recordings, isEmpty);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
  });

  testWidgets(
    'same recording can retry after STT failure without automatic retry',
    (tester) async {
      final service = Sc10Service()
        ..failure = const BackendFailure(
          BackendFailureKind.generic,
          code: 'STT_FAILED',
          retryable: true,
          stage: 'stt',
        );
      await mountVoice(tester, service, Sc10Recorder(), Sc10Player());
      await startRecording(tester);
      await finishRecording(tester);
      expect(service.recordings.length, 1);
      service.failure = null;
      await tester.ensureVisible(find.text('같은 녹음 다시 전송'));
      await tester.tap(find.text('같은 녹음 다시 전송'));
      await tester.pumpAndSettle();
      expect(service.recordings.length, 2);
      expect(identical(service.recordings[0], service.recordings[1]), true);
      expect(find.text('인식된 합성 질문'), findsOneWidget);
    },
  );

  testWidgets(
    'TTS partial failure keeps text and only retries speech, with disclosure',
    (tester) async {
      final service = Sc10Service()..result = sampleReply(ttsFailed: true);
      final player = Sc10Player();
      await mountVoice(tester, service, Sc10Recorder(), player, parent: true);
      await startRecording(tester);
      await finishRecording(tester);
      expect(find.text('음성 대화 합성 답변'), findsOneWidget);
      await acceptPlay(tester);
      expect(service.recordings.length, 1);
      expect(service.syntheses, 1);
      expect(player.plays, 1);
      expect(find.textContaining('실패했어요. 답변 글은'), findsNothing);
      player.done.add(null);
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('답변 음성 재생'));
      await tester.pumpAndSettle();
      expect(service.syntheses, 1);
    },
  );

  testWidgets(
    'audio supports pause, resume, progress and seeking and releases on leave',
    (tester) async {
      final service = Sc10Service();
      final player = Sc10Player();
      await mountVoice(tester, service, Sc10Recorder(), player);
      await startRecording(tester);
      await finishRecording(tester);
      await acceptPlay(tester);
      expect(player.plays, 1);
      player.durations.add(const Duration(seconds: 10));
      player.positions.add(const Duration(seconds: 2));
      await tester.pumpAndSettle();
      expect(find.text('0:02'), findsOneWidget);
      expect(find.byType(Slider), findsOneWidget);
      await tester.tap(find.byTooltip('음성 일시정지'));
      await tester.pumpAndSettle();
      expect(player.pauses, 1);
      await tester.tap(find.byTooltip('답변 음성 재생'));
      await tester.pumpAndSettle();
      expect(player.resumes, 1);
      tester.widget<Slider>(find.byType(Slider)).onChanged!(.5);
      await tester.pumpAndSettle();
      expect(player.sought, const Duration(seconds: 5));
      expect(service.syntheses, 0);
      await tester.pumpWidget(const SizedBox());
      await tester.pump();
      expect(player.disposed, true);
    },
  );

  testWidgets(
    'pending response blocks duplicate capture, times out, ignores late response',
    (tester) async {
      final service = Sc10Service()..pending = Completer<VoiceReply>();
      final player = Sc10Player();
      await mountVoice(tester, service, Sc10Recorder(), player);
      await startRecording(tester);
      await finishRecording(tester, pending: true);
      expect(service.recordings.length, 1);
      expect(
        tester
            .widget<IconButton>(
              find.byWidgetPredicate(
                (widget) => widget is IconButton && widget.tooltip == '눌러서 말하기',
              ),
            )
            .onPressed,
        isNull,
      );
      await tester.pump(const Duration(seconds: 31));
      await tester.pumpAndSettle();
      expect(find.text('응답을 받지 못했어요. 다시 시도해 주세요.'), findsOneWidget);
      service.pending!.complete(sampleReply());
      await tester.pumpAndSettle();
      expect(find.text('인식된 합성 질문'), findsNothing);
      expect(player.plays, 0);
    },
  );

  testWidgets('small screen can cancel recording while staying in voice mode', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final service = Sc10Service();
    final recorder = Sc10Recorder();
    await mountVoice(tester, service, recorder, Sc10Player(), parent: true);
    await startRecording(tester);
    expect(tester.takeException(), isNull);
    await tester.tap(find.byTooltip('녹음 취소'));
    await tester.pumpAndSettle();
    expect(recorder.cancels, 1);
    expect(service.recordings, isEmpty);
    expect(find.byType(VoiceScreen), findsOneWidget);
    expect(find.byTooltip('텍스트 대화로 전환'), findsNothing);
  });

  test('preview voice stores voice mode and transcript in memory without a microphone', () async {
    final auth = fixtures.TestAuth();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    final service = MemoryConversationService(
      backend: BackendService(auth),
      chat: const DemoChatService(),
    );
    final session = await service.create(DateTime.now(), voice: true);
    final result = await ConversationChatService(
      service,
      session.id,
    ).replyVoice(sampleRecording());
    expect(result.preview, true);
    final restored = await service.get(session.id);
    expect(restored.lastVoice, true);
    expect(restored.turns.single.question, result.transcript);
    expect(restored.messageCount, 2);
  });
}
