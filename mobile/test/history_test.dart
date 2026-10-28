import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/chat/chat_service.dart';
import 'package:mobile/chat/voice_screen.dart';
import 'package:mobile/history/conversation.dart';
import 'package:mobile/history/conversation_service.dart';
import 'package:mobile/history/history_screen.dart';
import 'package:mobile/main.dart';
import 'package:mobile/services/backend_service.dart';

class TestAuth extends PreviewAuthService {
  @override
  Future<Map<String, String>> authorizationHeaders() async => {
    'Authorization': 'Bearer synthetic-token',
  };
}

class InstantChat implements ChatService {
  @override
  bool get isDemo => true;
  @override
  Future<String> reply(String message) async => '합성 답변: $message';
}

class TestHistory implements ConversationService {
  List<Conversation> items = [];
  bool failList = false, failDelete = false, failGet = false;
  int deletes = 0, creates = 0;
  bool? createdVoice;
  final sends = <({String session, String turn, String message})>[];
  @override
  bool get isDemo => true;
  @override
  Future<List<Conversation>> list() async {
    if (failList) throw StateError('synthetic failure');
    return [...items];
  }

  @override
  Future<Conversation> get(String id) async {
    if (failGet) throw StateError('synthetic failure');
    return items.firstWhere((item) => item.id == id);
  }

  @override
  Future<void> delete(String id) async {
    deletes++;
    if (failDelete) throw StateError('synthetic failure');
    items.removeWhere((item) => item.id == id);
  }

  @override
  Future<Conversation> create(
    DateTime acknowledgedAt, {
    bool voice = false,
  }) async {
    creates++;
    createdVoice = voice;
    final item = conversation(id: 'new-$creates');
    items.add(item);
    return item;
  }

  @override
  Future<String> send(String id, String turnId, String message) async {
    sends.add((session: id, turn: turnId, message: message));
    return '새 합성 답변';
  }
}

Conversation conversation({
  String id = 'session-1',
  DateTime? date,
  bool voice = false,
  bool pending = false,
}) {
  final time = date ?? DateTime.now();
  return Conversation(
    id: id,
    personaId: 'persona-1',
    createdAt: time,
    updatedAt: time,
    acknowledgedAt: time,
    turns: [
      ConversationTurn(
        id: 'turn-1',
        number: 3,
        createdAt: time,
        question: '합성 첫 질문 $id',
        answer: pending ? null : '저장된 합성 답변',
        voice: voice,
        answerStatus: pending ? 'pending' : 'done',
      ),
    ],
  );
}

Map<String, dynamic> sessionJson(String id) => {
  'id': id,
  'persona_id': 'persona-1',
  'title': null,
  'created_at': '2026-10-28T01:00:00Z',
  'updated_at': '2026-10-28T02:00:00Z',
  'ai_notice_ack_at': '2026-10-28T01:00:00Z',
};
Map<String, dynamic> turnJson(int number) => {
  'turn_id': 'turn-$number',
  'turn_no': number,
  'question': '질문 $number',
  'answer': '답변 $number',
  'is_ai_generated': true,
  'input_mode': 'voice',
  'question_status': 'done',
  'answer_status': 'done',
  'created_at': '2026-10-28T02:00:00Z',
};
http.Response jsonResponse(Object value, {int status = 200}) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json; charset=utf-8'},
);

void main() {
  testWidgets(
    'empty voice conversation retains its mode through home and history, then follows the last sent turn',
    (tester) async {
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      await tester.pumpWidget(
        ReplicaApp(authService: auth, chatService: InstantChat()),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('음성으로 대화'));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(Checkbox));
      await tester.pumpAndSettle();
      await tester.tap(find.text('대화 시작'));
      await tester.pumpAndSettle();
      expect(find.byType(VoiceScreen), findsOneWidget);
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.textContaining(' · 음성'), findsOneWidget);
      await tester.tap(find.text('전체 보기'));
      await tester.pumpAndSettle();
      expect(find.textContaining('음성 · 메시지 0개'), findsOneWidget);
      await tester.tap(
        find.descendant(
          of: find.byType(ConversationHistoryTile),
          matching: find.text('새 대화'),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(VoiceScreen), findsOneWidget);
      expect(find.byTooltip('텍스트 대화로 전환'), findsNothing);
      await tester.tap(find.byTooltip('눌러서 말하기'));
      await tester.pump();
      await tester.tap(find.byTooltip('녹음 종료 후 전송'));
      await tester.pumpAndSettle();
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.textContaining('음성 · 메시지 2개'), findsOneWidget);
      await tester.tap(find.text('음성 대화 화면 확인용 합성 질문입니다.'));
      await tester.pumpAndSettle();
      expect(find.byType(VoiceScreen), findsOneWidget);
      expect(find.byTooltip('메시지 전송'), findsNothing);
      expect(find.byTooltip('텍스트 대화로 전환'), findsNothing);
    },
  );

  test('HTTP empty sessions retain their initial mode without adding draft API fields', () async {
    final auth = TestAuth();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    final client = MockClient((request) async {
      if (request.url.path == '/personas') {
        return jsonResponse({
          'items': [
            {'id': 'persona-1', 'status': 'active'},
          ],
        });
      }
      if (request.method == 'POST') {
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body.keys.toSet(), {
          'persona_id',
          'session_id',
          'ai_notice_ack_at',
        });
        return jsonResponse(sessionJson('voice-session'), status: 201);
      }
      if (request.url.path.endsWith('/turns')) {
        return jsonResponse({'items': [], 'limit': 100, 'after_turn_no': 0});
      }
      if (request.url.path == '/sessions') {
        return jsonResponse({
          'items': [sessionJson('voice-session')],
          'limit': 50,
          'offset': 0,
        });
      }
      return jsonResponse(sessionJson('voice-session'));
    });
    addTearDown(client.close);
    final service = HttpConversationService(
      backend: BackendService(auth),
      baseUrl: Uri.parse('http://localhost:8000'),
      client: client,
    );
    expect((await service.create(DateTime.now(), voice: true)).lastVoice, true);
    expect((await service.list()).single.lastVoice, true);
    expect((await service.get('voice-session')).lastVoice, true);
    final now = DateTime.now();
    final memory = MemoryConversationService(
      backend: BackendService(auth),
      chat: InstantChat(),
    );
    expect((await memory.create(now)).lastVoice, false);
    final voice = await memory.create(now, voice: true);
    expect((await memory.get(voice.id)).lastVoice, true);
  });

  test('date groups cover day, week, month and previous years', () {
    final now = DateTime(2026, 10, 28);
    expect(conversationDateGroup(now, now), '오늘');
    expect(conversationDateGroup(DateTime(2026, 10, 26), now), '이번 주');
    expect(conversationDateGroup(DateTime(2026, 10, 25), now), '이번 달');
    expect(conversationDateGroup(DateTime(2026, 9, 30), now), '9월');
    expect(conversationDateGroup(DateTime(2025, 10, 28), now), '2025년 10월');
  });

  test(
    'HTTP adapter uses auth, offset and sparse turn cursors; derives metadata',
    () async {
      final auth = TestAuth();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      final paths = <String>[];
      final client = MockClient((request) async {
        expect(request.headers['Authorization'], 'Bearer synthetic-token');
        expect(request.url.queryParameters.containsKey('user_id'), false);
        paths.add(request.url.toString());
        if (request.url.path == '/sessions') {
          final offset = request.url.queryParameters['offset'];
          return jsonResponse({
            'items': offset == '0' ? [sessionJson('s1')] : [],
            'limit': 1,
            'offset': int.parse(offset!),
          });
        }
        final after = int.parse(request.url.queryParameters['after_turn_no']!);
        return jsonResponse({
          'items': after == 0
              ? [turnJson(3)]
              : after == 3
              ? [turnJson(9)]
              : [],
          'limit': 1,
          'after_turn_no': after,
        });
      });
      addTearDown(client.close);
      final service = HttpConversationService(
        backend: BackendService(auth),
        baseUrl: Uri.parse('http://localhost:8000'),
        client: client,
      );
      final items = await service.list();
      expect(items.single.firstQuestion, '질문 3');
      expect(items.single.lastVoice, true);
      expect(items.single.messageCount, 4);
      expect(items.single.turns.map((turn) => turn.number), [3, 9]);
      expect(paths.any((url) => url.contains('offset=1')), true);
      expect(paths.any((url) => url.contains('after_turn_no=9')), true);
    },
  );

  test('continuation retries reuse turn ID and successful next message gets a new ID', () async {
    final service = TestHistory();
    final chat = ConversationChatService(service, 'same-session');
    // Simulate transport failure using an HTTP mock below to exercise the adapter too.
    final auth = TestAuth();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    final bodies = <Map<String, dynamic>>[];
    final client = MockClient((request) async {
      final body = jsonDecode(request.body) as Map<String, dynamic>;
      bodies.add(body);
      if (bodies.length == 1) {
        return jsonResponse({
          'error': {'code': 'AI_TIMEOUT', 'retryable': true},
        }, status: 504);
      }
      return jsonResponse({
        'turn_id': body['turn_id'],
        'answer': '합성 답변',
        'is_ai_generated': true,
      });
    });
    addTearDown(client.close);
    final httpChat = ConversationChatService(
      HttpConversationService(
        backend: BackendService(auth),
        baseUrl: Uri.parse('http://localhost:8000'),
        client: client,
      ),
      'same-session',
    );
    await expectLater(httpChat.reply('질문'), throwsA(isA<BackendFailure>()));
    await httpChat.reply('질문');
    await httpChat.reply('다음 질문');
    expect(bodies[0]['turn_id'], bodies[1]['turn_id']);
    expect(bodies[1]['turn_id'], isNot(bodies[2]['turn_id']));
    expect(
      bodies.every(
        (body) =>
            body['session_id'] == 'same-session' &&
            !body.containsKey('user_id'),
      ),
      true,
    );
    await chat.reply('합성 질문');
    expect(service.sends.single.session, 'same-session');
  });

  test('delayed response from old account is discarded and 401 invalidates session', () async {
    final auth = TestAuth();
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
    final check = expectLater(service.list(), throwsA(isA<BackendFailure>()));
    await Future<void>.delayed(Duration.zero);
    await auth.signOut();
    await auth.signIn(
      PreviewAuthService.emails.last,
      PreviewAuthService.password,
    );
    pending.complete(
      jsonResponse({
        'items': [sessionJson('private-A')],
        'limit': 50,
        'offset': 0,
      }),
    );
    await check;
    expect(auth.identity!.email, PreviewAuthService.emails.last);
    final unauthorized = MockClient(
      (_) async => http.Response('unauthorized', 401),
    );
    addTearDown(unauthorized.close);
    await expectLater(
      HttpConversationService(
        backend: BackendService(auth),
        baseUrl: Uri.parse('http://localhost:8000'),
        client: unauthorized,
      ).list(),
      throwsA(isA<BackendFailure>()),
    );
    expect(auth.identity, isNull);
  });

  test('DELETE requires 204 and accepts empty body', () async {
    final auth = TestAuth();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    var status = 202;
    final client = MockClient((request) async {
      expect(request.method, 'DELETE');
      expect(request.url.path, '/sessions/s1');
      return http.Response('', status);
    });
    addTearDown(client.close);
    final service = HttpConversationService(
      backend: BackendService(auth),
      baseUrl: Uri.parse('http://localhost:8000'),
      client: client,
    );
    await expectLater(service.delete('s1'), throwsFormatException);
    status = 204;
    await service.delete('s1');
  });

  testWidgets('empty state starts a new conversation only after AI consent', (
    tester,
  ) async {
    final service = TestHistory();
    await tester.pumpWidget(MaterialApp(home: HistoryScreen(service: service)));
    await tester.pumpAndSettle();
    expect(find.text('아직 나눈 대화가 없어요'), findsOneWidget);
    await tester.tap(find.text('새 대화').first);
    await tester.pumpAndSettle();
    expect(find.text('어떤 방식으로 대화할까요?'), findsOneWidget);
    expect(service.creates, 0);
    await tester.tap(find.text('텍스트로 대화'));
    await tester.pumpAndSettle();
    expect(service.creates, 0);
    await tester.tap(find.byType(Checkbox));
    await tester.pumpAndSettle();
    await tester.tap(find.text('대화 시작'));
    await tester.pumpAndSettle();
    expect(service.creates, 1);
    expect(service.createdVoice, false);
    expect(find.byTooltip('메시지 전송'), findsOneWidget);
  });

  testWidgets('history new conversation can select voice before AI consent', (
    tester,
  ) async {
    final service = TestHistory()..items = [conversation()];
    await tester.pumpWidget(MaterialApp(home: HistoryScreen(service: service)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('새 대화'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('음성으로 대화'));
    await tester.pumpAndSettle();
    expect(service.creates, 0);
    await tester.tap(find.byType(Checkbox));
    await tester.pumpAndSettle();
    await tester.tap(find.text('대화 시작'));
    await tester.pumpAndSettle();
    expect(service.creates, 1);
    expect(service.createdVoice, true);
    expect(find.byType(VoiceScreen), findsOneWidget);
  });

  testWidgets(
    'cancelling mode selection or AI consent creates no conversation',
    (tester) async {
      final service = TestHistory();
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(320, 568);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      await tester.pumpWidget(
        MaterialApp(home: HistoryScreen(service: service)),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('새 대화').first);
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(service.creates, 0);
      await tester.tap(find.text('새 대화').first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('음성으로 대화'));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('닫기'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('닫기'));
      await tester.pumpAndSettle();
      expect(service.creates, 0);
      expect(find.byType(Checkbox), findsNothing);
      expect(find.text('아직 나눈 대화가 없어요'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('list failure retries; detail failure keeps list and retries', (
    tester,
  ) async {
    final service = TestHistory()..failList = true;
    await tester.pumpWidget(MaterialApp(home: HistoryScreen(service: service)));
    await tester.pumpAndSettle();
    expect(find.text('대화 이력을 불러오지 못했어요.'), findsOneWidget);
    service.failList = false;
    service.items = [conversation()];
    await tester.tap(find.text('다시 시도'));
    await tester.pumpAndSettle();
    service.failGet = true;
    await tester.tap(find.text('합성 첫 질문 session-1'));
    await tester.pumpAndSettle();
    expect(find.text('대화를 불러오지 못했어요.'), findsOneWidget);
    service.failGet = false;
    await tester.tap(find.text('다시 시도'));
    await tester.pumpAndSettle();
    expect(find.text('저장된 합성 답변'), findsOneWidget);
    expect(find.text('AI 생성'), findsOneWidget);
    await tester.enterText(find.byType(TextField), '이어가는 질문');
    await tester.pump();
    await tester.tap(find.byTooltip('메시지 전송'));
    await tester.pumpAndSettle();
    expect(service.sends.single.session, 'session-1');
    expect(find.text('AI 생성'), findsNWidgets(2));
  });

  testWidgets(
    'swipe exposes delete; cancel keeps row; failure restores row; retry removes',
    (tester) async {
      final service = TestHistory()
        ..items = [conversation()]
        ..failDelete = true;
      await tester.pumpWidget(
        MaterialApp(home: HistoryScreen(service: service)),
      );
      await tester.pumpAndSettle();
      await tester.drag(
        find.byType(ConversationHistoryTile),
        const Offset(-180, 0),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('삭제').first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(service.deletes, 0);
      await tester.tap(find.byTooltip('대화 삭제'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(TextButton, '삭제'));
      await tester.pumpAndSettle();
      expect(find.text('합성 첫 질문 session-1'), findsOneWidget);
      expect(find.textContaining('이력은 그대로 유지돼요'), findsOneWidget);
      service.failDelete = false;
      await tester.tap(find.text('다시 시도'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(TextButton, '삭제'));
      await tester.pumpAndSettle();
      expect(service.deletes, 2);
      expect(find.text('아직 나눈 대화가 없어요'), findsOneWidget);
    },
  );

  testWidgets('voice history resumes in voice mode without a text switch', (
    tester,
  ) async {
    final service = TestHistory()..items = [conversation(voice: true)];
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(MaterialApp(home: HistoryScreen(service: service)));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    await tester.tap(find.text('합성 첫 질문 session-1'));
    await tester.pumpAndSettle();
    expect(find.byType(VoiceScreen), findsOneWidget);
    expect(find.byTooltip('텍스트 대화로 전환'), findsNothing);
    expect(find.text('저장된 합성 답변'), findsOneWidget);
    expect(find.text('AI 생성'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('pending history blocks duplicate sends', (tester) async {
    final service = TestHistory()..items = [conversation(pending: true)];
    await tester.pumpWidget(MaterialApp(home: HistoryScreen(service: service)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('합성 첫 질문 session-1'));
    await tester.pumpAndSettle();
    expect(find.textContaining('아직 처리 중인 메시지'), findsOneWidget);
    expect(tester.widget<TextField>(find.byType(TextField)).readOnly, true);
    expect(
      tester
          .widget<IconButton>(
            find.widgetWithIcon(IconButton, Icons.send_outlined),
          )
          .onPressed,
      isNull,
    );
  });

  testWidgets(
    'home stores and reopens history; account switch clears previous history',
    (tester) async {
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      await tester.pumpWidget(
        ReplicaApp(authService: auth, chatService: InstantChat()),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('텍스트로 대화'));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(Checkbox));
      await tester.pumpAndSettle();
      await tester.tap(find.text('대화 시작'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), 'A 계정 합성 대화');
      await tester.pump();
      await tester.tap(find.byTooltip('메시지 전송'));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('뒤로'));
      await tester.pumpAndSettle();
      expect(find.text('A 계정 합성 대화'), findsOneWidget);
      await tester.tap(find.text('전체 보기'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('A 계정 합성 대화'));
      await tester.pumpAndSettle();
      expect(find.text('합성 답변: A 계정 합성 대화'), findsOneWidget);
      await auth.signOut();
      await auth.signIn(
        PreviewAuthService.emails.last,
        PreviewAuthService.password,
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('전체 보기'));
      await tester.pumpAndSettle();
      expect(find.text('아직 나눈 대화가 없어요'), findsOneWidget);
      expect(find.text('A 계정 합성 대화'), findsNothing);
    },
  );
}
