import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/chat/chat_screen.dart';
import 'package:mobile/chat/chat_service.dart';
import 'package:mobile/chat/reply_audio_player.dart';
import 'package:mobile/chat/reply_evidence.dart';
import 'package:mobile/history/conversation_service.dart';
import 'package:mobile/services/backend_service.dart';

import 'history_test.dart' as fixtures;

class Sc09Service implements DetailedChatService {
  ChatReply response = const ChatReply(text: '합성 답변', turnId: 'turn-synthetic');
  Object? replyFailure, speechFailure;
  Completer<SpeechAudio>? speechPending;
  Completer<ChatReply>? replyPending;
  int sends = 0, syntheses = 0;
  @override
  bool get isDemo => false;
  @override
  Future<String> reply(String message) async =>
      (await replyDetailed(message)).text;
  @override
  Future<ChatReply> replyDetailed(String message) async {
    sends++;
    if (replyFailure != null) throw replyFailure!;
    return replyPending?.future ?? response;
  }

  @override
  Future<SpeechAudio> synthesize(ChatReply reply) async {
    syntheses++;
    if (speechFailure != null) throw speechFailure!;
    return speechPending?.future ??
        SpeechAudio(Uint8List.fromList([1, 2, 3]), 'audio/wav');
  }
}

class Sc09Player implements ReplyAudioPlayer {
  final done = StreamController<void>.broadcast();
  final failures = StreamController<Object>.broadcast();
  int plays = 0, stops = 0;
  bool disposed = false;
  @override
  Stream<void> get completed => done.stream;
  @override
  Stream<Object> get errors => failures.stream;
  @override
  Future<void> play(SpeechAudio audio) async {
    plays++;
  }

  @override
  Future<void> stop() async {
    stops++;
  }

  @override
  Future<void> dispose() async {
    disposed = true;
    await done.close();
    await failures.close();
  }
}

Future<void> openAndSend(
  WidgetTester tester,
  Sc09Service service, {
  Sc09Player? player,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: ChatScreen(service: service, audioPlayer: player),
    ),
  );
  await tester.enterText(find.byType(TextField), '합성 질문');
  await tester.pump();
  await tester.tap(find.byTooltip('메시지 전송'));
  await tester.pump();
  if (service.replyPending == null) await tester.pumpAndSettle();
}

Future<void> acceptSpeech(WidgetTester tester) async {
  await tester.tap(find.text('듣기'));
  await tester.pumpAndSettle();
  expect(find.text('합성한 목소리예요'), findsOneWidget);
  await tester.tap(find.widgetWithText(FilledButton, '재생'));
  await tester.pump();
}

void main() {
  testWidgets('evidence opens per-answer filename, date and original excerpt', (
    tester,
  ) async {
    final service = Sc09Service()
      ..response = ChatReply(
        text: '근거가 있는 합성 답변',
        evidence: EvidenceState.available,
        turnId: 'turn-synthetic',
        sources: [
          EvidenceRecord(
            fileName: '합성 자료.txt',
            date: DateTime(2026, 10, 3),
            excerpt: '검증용 원문 조각입니다.',
          ),
        ],
      );
    await openAndSend(tester, service);
    expect(find.text('AI 생성'), findsOneWidget);
    await tester.tap(find.text('근거 기록 1'));
    await tester.pumpAndSettle();
    expect(find.text('합성 자료.txt'), findsOneWidget);
    expect(find.text('2026년 10월 3일'), findsOneWidget);
    expect(find.text('검증용 원문 조각입니다.'), findsOneWidget);
    await tester.tap(find.byTooltip('근거 기록 닫기'));
    await tester.pumpAndSettle();
    expect(find.text('근거가 있는 합성 답변'), findsOneWidget);
  });

  testWidgets(
    'explicit no evidence is dashed; absent evidence is kept unknown',
    (tester) async {
      final service = Sc09Service()
        ..response = const ChatReply(
          text: '기록에서 찾을 수 없어요.',
          evidence: EvidenceState.none,
        );
      await openAndSend(tester, service);
      expect(find.text('근거 기록 없음'), findsOneWidget);
      expect(
        find.byWidgetPredicate(
          (widget) =>
              widget is CustomPaint &&
              widget.foregroundPainter is NoEvidenceBorder,
        ),
        findsOneWidget,
      );
      service.response = const ChatReply(text: '근거 메타데이터가 없는 답변');
      await tester.enterText(find.byType(TextField), '다음 질문');
      await tester.pump();
      await tester.tap(find.byTooltip('메시지 전송'));
      await tester.pumpAndSettle();
      expect(find.text('근거 정보 미제공'), findsOneWidget);
      expect(
        find.byWidgetPredicate(
          (widget) =>
              widget is CustomPaint &&
              widget.foregroundPainter is NoEvidenceBorder,
        ),
        findsOneWidget,
      );
      expect(find.text('AI 생성'), findsNWidgets(2));
    },
  );

  testWidgets('length-limited answer stays visible with its notice', (
    tester,
  ) async {
    final service = Sc09Service()
      ..response = const ChatReply(
        text: '길이 제한으로 잘린 합성 답변',
        finishReason: 'length',
      );
    await openAndSend(tester, service);
    expect(find.text('길이 제한으로 잘린 합성 답변'), findsOneWidget);
    expect(find.textContaining('길이 제한으로 일부 생략'), findsOneWidget);
    expect(find.text('응답을 받지 못했어요.'), findsNothing);
  });

  testWidgets(
    'speech requires disclosure, can cancel, replays cached audio and stops on dispose',
    (tester) async {
      final service = Sc09Service();
      final player = Sc09Player();
      await openAndSend(tester, service, player: player);
      await tester.tap(find.text('듣기'));
      await tester.pumpAndSettle();
      expect(service.syntheses, 0);
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(player.plays, 0);
      await acceptSpeech(tester);
      await tester.pumpAndSettle();
      expect(service.syntheses, 1);
      expect(player.plays, 1);
      expect(find.text('재생 중지'), findsOneWidget);
      await tester.tap(find.text('재생 중지'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('듣기'));
      await tester.pumpAndSettle();
      expect(find.text('합성한 목소리예요'), findsNothing);
      expect(service.syntheses, 1);
      expect(player.plays, 2);
      player.done.add(null);
      await tester.pumpAndSettle();
      expect(find.text('듣기'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
      await tester.pump();
      expect(player.disposed, true);
    },
  );

  testWidgets(
    'TTS failure preserves text and retries without sending a new chat',
    (tester) async {
      final service = Sc09Service()
        ..speechFailure = const BackendFailure(
          BackendFailureKind.network,
          retryable: true,
        );
      final player = Sc09Player();
      await openAndSend(tester, service, player: player);
      await acceptSpeech(tester);
      await tester.pumpAndSettle();
      expect(find.text('합성 답변'), findsOneWidget);
      expect(find.textContaining('답변 글은 그대로 볼 수 있어요'), findsOneWidget);
      service.speechFailure = null;
      await tester.tap(find.text('듣기 다시 시도'));
      await tester.pumpAndSettle();
      expect(service.sends, 1);
      expect(service.syntheses, 2);
      expect(player.plays, 1);
    },
  );

  testWidgets(
    'late TTS completion after leaving screen never starts playback',
    (tester) async {
      final service = Sc09Service()..speechPending = Completer<SpeechAudio>();
      final player = Sc09Player();
      await openAndSend(tester, service, player: player);
      await acceptSpeech(tester);
      await tester.pump();
      expect(find.text('음성 준비 중'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
      service.speechPending!.complete(SpeechAudio(Uint8List(1), 'audio/wav'));
      await tester.pumpAndSettle();
      expect(player.plays, 0);
      expect(player.disposed, true);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('leaving app stops playback and invalidates pending synthesis', (
    tester,
  ) async {
    final service = Sc09Service()..speechPending = Completer<SpeechAudio>();
    final player = Sc09Player();
    await openAndSend(tester, service, player: player);
    await acceptSpeech(tester);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    service.speechPending!.complete(SpeechAudio(Uint8List(1), 'audio/wav'));
    await tester.pumpAndSettle();
    expect(player.plays, 0);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
  });

  testWidgets(
    'non-retryable backend error offers edit and does not expose raw message',
    (tester) async {
      final service = Sc09Service()
        ..replyFailure = BackendFailure.fromResponse(
          statusCode: 422,
          body: {
            'error': {
              'code': 'MESSAGE_TOO_LONG',
              'retryable': false,
              'message': 'private diagnostic',
            },
          },
        );
      await openAndSend(tester, service);
      expect(find.text('질문이 너무 길어요. 더 짧게 입력해 주세요.'), findsOneWidget);
      expect(find.text('다시 시도'), findsNothing);
      expect(find.text('private diagnostic'), findsNothing);
      expect(find.text('합성 질문'), findsOneWidget);
      await tester.tap(find.text('메시지 수정'));
      await tester.pumpAndSettle();
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        '합성 질문',
      );
    },
  );

  testWidgets(
    '30-second timeout enables manual retry and ignores a late answer',
    (tester) async {
      final service = Sc09Service()..replyPending = Completer<ChatReply>();
      await openAndSend(tester, service);
      await tester.pump(const Duration(seconds: 31));
      await tester.pumpAndSettle();
      expect(find.text('응답을 받지 못했어요.'), findsOneWidget);
      expect(service.sends, 1);
      service.replyPending!.complete(const ChatReply(text: '늦은 합성 답변'));
      await tester.pumpAndSettle();
      expect(find.text('늦은 합성 답변'), findsNothing);
      service.replyPending = null;
      await tester.tap(find.text('다시 시도'));
      await tester.pumpAndSettle();
      expect(service.sends, 2);
      expect(find.text('합성 질문'), findsOneWidget);
    },
  );

  test('HTTP detailed reply preserves timing and finish reason and uses authenticated turn TTS', () async {
    final auth = fixtures.TestAuth();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    final client = MockClient((request) async {
      expect(request.headers['Authorization'], 'Bearer synthetic-token');
      if (request.url.path == '/chat/text') {
        expect(jsonDecode(request.body), {
          'session_id': 'session-1',
          'turn_id': 'turn-1',
          'message': '합성 질문',
        });
        return fixtures.jsonResponse({
          'turn_id': 'turn-1',
          'answer': '합성 답변',
          'is_ai_generated': true,
          'finish_reason': 'length',
          'model_name': 'synthetic-model',
          'timings': {'llm_ms': 125, 'total_ms': 150},
        });
      }
      if (request.url.path == '/chat/turns/turn-1/tts') {
        expect(request.method, 'POST');
        expect(request.body, isEmpty);
        return fixtures.jsonResponse({
          'turn_id': 'turn-1',
          'tts_status': 'ready',
          'audio_base64': base64Encode([1, 2, 3]),
          'audio_format': 'wav',
        });
      }
      if (request.url.path.endsWith('/turns')) {
        return fixtures.jsonResponse({
          'items': [
            {...fixtures.turnJson(1), 'answer': '합성 답변'},
          ],
          'limit': 100,
          'after_turn_no': 0,
        });
      }
      return fixtures.jsonResponse(fixtures.sessionJson('session-1'));
    });
    addTearDown(client.close);
    final service = HttpConversationService(
      backend: BackendService(auth),
      baseUrl: Uri.parse('http://localhost:8000'),
      client: client,
    );
    final reply = await service.sendDetailed('session-1', 'turn-1', '합성 질문');
    expect(reply.timings['llm_ms'], 125);
    expect(reply.finishReason, 'length');
    expect(reply.evidence, EvidenceState.unknown);
    final restored = (await service.get('session-1')).replyMetadata[1]!;
    expect(restored.finishReason, 'length');
    expect((await service.synthesize(restored)).bytes, [1, 2, 3]);
  });

  test('invalid AI or speech data is rejected and absent evidence is never inferred as none', () {
    final json = {
      'turn_id': 't',
      'answer': '합성 답변',
      'is_ai_generated': true,
      'finish_reason': 'stop',
    };
    expect(
      ChatReply.fromJson(json, expectedTurnId: 't').evidence,
      EvidenceState.unknown,
    );
    for (final patch in [
      {'turn_id': 'other'},
      {'is_ai_generated': false},
      {'finish_reason': 'invalid'},
      {'answer': ' '},
    ]) {
      expect(
        () => ChatReply.fromJson({...json, ...patch}, expectedTurnId: 't'),
        throwsFormatException,
      );
    }
    for (final patch in [
      {'turn_id': 'other'},
      {'tts_status': 'failed'},
      {'audio_format': 'unknown'},
      {'audio_base64': ''},
    ]) {
      expect(
        () => SpeechAudio.fromJson({
          'turn_id': 't',
          'tts_status': 'ready',
          'audio_base64': base64Encode([1]),
          'audio_format': 'wav',
          ...patch,
        }, 't'),
        throwsFormatException,
      );
    }
  });

  test('memory history retains evidence metadata when reopening', () async {
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
    final conversation = await service.create(DateTime.now());
    await service.sendDetailed(conversation.id, 'turn-1', '합성 질문');
    final reply = (await service.get(conversation.id)).replyMetadata[1]!;
    expect(reply.sources.single.fileName, '합성 예시 기록.txt');
    expect(reply.evidence, EvidenceState.available);
    await service.sendDetailed(conversation.id, 'turn-2', '기록 없는 질문');
    expect(
      (await service.get(conversation.id)).replyMetadata[3]!.evidence,
      EvidenceState.none,
    );
  });
}
