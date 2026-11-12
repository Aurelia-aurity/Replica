import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/chat/chat_screen.dart';
import 'package:mobile/chat/chat_service.dart';
import 'package:mobile/history/history_screen.dart';
import 'package:mobile/main.dart';
import 'package:mobile/profile/persona_models.dart';
import 'package:mobile/profile/persona_name_screen.dart';
import 'package:mobile/profile/persona_service.dart';

import 'history_test.dart' as fixtures;
import 'persona_test_support.dart';
import 'sc09_test.dart' as sc09;
import 'sc10_test.dart' as sc10;

const mother = Persona(id: 'p1', name: '엄마');

void smallScreen(WidgetTester tester) {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = const Size(320, 568);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.view.resetPhysicalSize);
}

class QuietChat implements ChatService {
  @override
  bool get isDemo => true;
  @override
  Future<String> reply(String message) async => '합성 답변';
}

Future<void> pumpApp(
  WidgetTester tester, {
  PersonaService Function()? factory,
}) async {
  final auth = PreviewAuthService();
  addTearDown(auth.dispose);
  await auth.signIn(
    PreviewAuthService.emails.first,
    PreviewAuthService.password,
  );
  await tester.pumpWidget(
    ReplicaApp(
      authService: auth,
      chatService: QuietChat(),
      personaServiceFactory: factory,
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> acceptDisclosure(WidgetTester tester) async {
  await tester.tap(find.byType(Checkbox));
  await tester.pumpAndSettle();
  await tester.tap(find.text('대화 시작'));
  await tester.pumpAndSettle();
}

Finder titleIn(Type screen, String name) =>
    find.descendant(of: find.byType(screen), matching: find.text(name));

void main() {
  group('home card', () {
    testWidgets('the dummy account starts without a partner, like a real one', (
      tester,
    ) async {
      await pumpApp(tester); // no injected partner: the dummy default
      expect(find.text('프로필을 만들어 주세요'), findsOneWidget);
      expect(find.text('엄마'), findsNothing);
      expect(find.text('텍스트로 대화'), findsNothing);
      expect(find.text('음성으로 대화'), findsNothing);
      expect(find.text('프로필 만들기'), findsOneWidget);
    });

    testWidgets('the dummy account names a partner, then converses', (
      tester,
    ) async {
      await pumpApp(tester);
      await nameFirstPartner(tester, '할머니');
      expect(find.text('할머니'), findsOneWidget);
      expect(find.text('프로필을 만들어 주세요'), findsNothing);
      await tester.tap(find.text('텍스트로 대화'));
      await tester.pumpAndSettle();
      await acceptDisclosure(tester);
      expect(titleIn(ChatScreen, '할머니'), findsOneWidget);
    });

    testWidgets('an existing partner has no way into a profile screen', (
      tester,
    ) async {
      await pumpApp(
        tester,
        factory: () => FakePersonaService(personas: const [mother]),
      );
      expect(find.text('엄마'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsOneWidget);
      expect(find.byIcon(Icons.chevron_right), findsNothing);
      await tester.tap(find.text('엄마'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.text('텍스트로 대화'), findsOneWidget);
    });

    testWidgets('no partner: the card offers SC-13 instead of conversations', (
      tester,
    ) async {
      final service = FakePersonaService();
      await pumpApp(tester, factory: () => service);
      expect(find.text('프로필을 만들어 주세요'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsNothing);

      await tester.tap(find.text('프로필 만들기'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsOneWidget);
      // The request lives on the card only; SC-13 words it differently.
      expect(find.text('프로필을 만들어 주세요'), findsNothing);
      await tester.enterText(find.byType(TextField), '할머니');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, '저장'));
      await tester.pumpAndSettle();

      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.text('할머니'), findsOneWidget);
      expect(find.text('프로필을 만들어 주세요'), findsNothing);
      expect(find.text('텍스트로 대화'), findsOneWidget);
      expect(service.created.single.name, '할머니');
      expect(service.listCalls, 1); // saving does not list again
    });

    testWidgets('cancelling SC-13 leaves no partner behind', (tester) async {
      final service = FakePersonaService();
      await pumpApp(tester, factory: () => service);
      await tester.tap(find.text('프로필 만들기'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '할머니');
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(find.text('프로필을 만들어 주세요'), findsOneWidget);
      expect(service.created, isEmpty);
    });

    testWidgets('a failed partner list can be retried from the card', (
      tester,
    ) async {
      final service = FakePersonaService(personas: const [mother])
        ..listError = StateError('offline');
      await pumpApp(tester, factory: () => service);
      expect(find.text('대화 상대를 불러오지 못했어요.'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsNothing);
      expect(find.text('프로필을 만들어 주세요'), findsNothing);
      service.listError = null;
      await tester.tap(find.text('다시 시도'));
      await tester.pumpAndSettle();
      expect(find.text('엄마'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsOneWidget);
    });

    testWidgets('fits 320 wide with the longest name', (tester) async {
      smallScreen(tester);
      final service = FakePersonaService(
        personas: [Persona(id: 'p1', name: '가' * 50)],
      );
      await pumpApp(tester, factory: () => service);
      expect(tester.takeException(), isNull);
      expect(find.text('텍스트로 대화'), findsOneWidget);
    });

    testWidgets('the no-partner card fits 320 wide', (tester) async {
      smallScreen(tester);
      await pumpApp(tester);
      expect(tester.takeException(), isNull);
      expect(find.text('프로필을 만들어 주세요'), findsOneWidget);
    });
  });

  group('the saved name in the conversation screens', () {
    testWidgets('home text button opens a chat titled with the name', (
      tester,
    ) async {
      await pumpApp(tester, factory: () => FakePersonaService());
      await nameFirstPartner(tester, '할머니');
      await tester.tap(find.text('텍스트로 대화'));
      await tester.pumpAndSettle();
      await acceptDisclosure(tester);
      expect(find.byType(ChatScreen), findsOneWidget);
      expect(titleIn(ChatScreen, '할머니'), findsOneWidget);
    });

    testWidgets('the conversations tab opens chats with the same name', (
      tester,
    ) async {
      await pumpApp(tester, factory: () => FakePersonaService());
      await nameFirstPartner(tester, '할머니');
      await tester.tap(find.text('전체 보기'));
      await tester.pumpAndSettle();
      await tester.tap(
        find.descendant(of: find.byType(AppBar), matching: find.text('새 대화')),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('텍스트로 대화'));
      await tester.pumpAndSettle();
      await acceptDisclosure(tester);
      expect(find.byType(ChatScreen), findsOneWidget);
      expect(titleIn(ChatScreen, '할머니'), findsOneWidget);
    });

    testWidgets('a 50 character name keeps the text title on one line', (
      tester,
    ) async {
      smallScreen(tester);
      await tester.pumpWidget(
        MaterialApp(
          home: ChatScreen(service: QuietChat(), conversationName: '가' * 50),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(find.text('가' * 50), findsOneWidget);
    });

    testWidgets('a 50 character name fits the voice title and AI badge', (
      tester,
    ) async {
      smallScreen(tester);
      await tester.pumpWidget(
        MaterialApp(
          home: ChatScreen(
            service: QuietChat(),
            conversationName: '가' * 50,
            initialVoiceMode: true,
            audioPlayer: sc09.Sc09Player(),
            voiceRecorder: sc10.Sc10Recorder(),
            voiceAudioPlayer: sc10.Sc10Player(),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(find.text('가' * 50), findsOneWidget);
      expect(find.text('AI 생성 대화'), findsOneWidget);
    });
  });

  group('without a named partner no conversation can start or open', () {
    testWidgets('new conversation explains instead of starting', (
      tester,
    ) async {
      final service = fixtures.TestHistory();
      await tester.pumpWidget(
        MaterialApp(home: HistoryScreen(service: service)),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('새 대화').first);
      await tester.pumpAndSettle();
      expect(find.text(partnerMissingNotice), findsOneWidget);
      expect(find.text('어떤 방식으로 대화할까요?'), findsNothing);
      expect(service.creates, 0);
    });

    testWidgets(
      'an existing conversation is not opened under a stand-in title',
      (tester) async {
        final service = fixtures.TestHistory()
          ..items = [fixtures.conversation()];
        await tester.pumpWidget(
          MaterialApp(home: HistoryScreen(service: service)),
        );
        await tester.pumpAndSettle();
        await tester.tap(find.textContaining('합성 첫 질문'));
        await tester.pumpAndSettle();
        expect(find.text(partnerMissingNotice), findsOneWidget);
        expect(find.byType(ChatScreen), findsNothing);
      },
    );

    testWidgets('no stand-in name is left in the conversation screens', (
      tester,
    ) async {
      // A conversation title is always the partner's name: it is required.
      await tester.pumpWidget(
        MaterialApp(
          home: ChatScreen(service: QuietChat(), conversationName: '합성 상대'),
        ),
      );
      expect(find.text('합성 상대'), findsOneWidget);
      expect(find.text('엄마'), findsNothing);
      expect(find.text('대화'), findsNothing);
    });
  });
}
