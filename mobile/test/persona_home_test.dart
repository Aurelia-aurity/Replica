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
import 'package:mobile/profile/speech_profile_screen.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_review.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/record_upload_screen.dart';
import 'package:mobile/records/records_panel.dart';

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

/// Preview records whose list is one record named [fileName].
class NamedRecords extends PreviewRecordService {
  NamedRecords(this.fileName);
  final String fileName;

  /// How often the list was requested.
  int listCalls = 0;

  @override
  Future<List<RecordEntry>> list() async {
    listCalls++;
    return [
      RecordEntry(
        id: fileName,
        name: fileName,
        kind: RecordKind.text,
        status: RecordStatus.ready,
        createdAt: DateTime.utc(2026, 11, 1),
        sizeBytes: 10,
      ),
    ];
  }
}

Future<void> pumpApp(
  WidgetTester tester, {
  PersonaService Function()? factory,
  RecordService Function(String? personaId)? records,
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
      recordServiceFactory: records,
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
    testWidgets('the card and the chat title use the display name', (
      tester,
    ) async {
      await pumpApp(
        tester,
        factory: () => FakePersonaService(
          personas: const [Persona(id: 'p1', name: '김영희', displayName: '엄마')],
        ),
      );
      expect(find.text('엄마'), findsOneWidget);
      expect(find.text('김영희'), findsNothing);
      await tester.tap(find.text('텍스트로 대화'));
      await tester.pumpAndSettle();
      await acceptDisclosure(tester);
      expect(titleIn(ChatScreen, '엄마'), findsOneWidget);
    });

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

    testWidgets('an existing partner leads to SC-07, not to SC-13', (
      tester,
    ) async {
      final service = FakePersonaService(personas: const [mother]);
      await pumpApp(tester, factory: () => service);
      expect(find.text('엄마'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsOneWidget);
      await tester.tap(find.text('말투 프로필'));
      await tester.pumpAndSettle();
      expect(find.byType(SpeechProfileScreen), findsOneWidget);
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.text('엄마의 말투'), findsOneWidget);
      expect(service.profileCalls, 1);
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.text('텍스트로 대화'), findsOneWidget);
    });

    testWidgets('SC-07 record upload opens SC-04 and rechecks on return', (
      tester,
    ) async {
      final service = FakePersonaService(personas: const [mother]);
      await pumpApp(tester, factory: () => service);
      await tester.tap(find.text('말투 프로필'));
      await tester.pumpAndSettle();
      expect(find.text('아직 분석한 기록이 없어요'), findsOneWidget);
      expect(service.profileCalls, 1);

      await tester.tap(find.text('기록 올리기'));
      await tester.pumpAndSettle();
      expect(find.byType(RecordUploadScreen), findsOneWidget);
      expect(find.text('기록 올리기는 아직 연결되지 않았어요.'), findsNothing);

      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.byType(SpeechProfileScreen), findsOneWidget);
      expect(service.profileCalls, 2);
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

      // Saving moves on to SC-07 for the new partner (issue #94).
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.byType(SpeechProfileScreen), findsOneWidget);
      expect(find.text('할머니의 말투'), findsOneWidget);
      await tester.pageBack();
      await tester.pumpAndSettle();
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

    /// Starts a text chat and comes back to the home tab.
    Future<void> chatOnce(WidgetTester tester) async {
      await tester.tap(find.text('텍스트로 대화'));
      await tester.pumpAndSettle();
      await acceptDisclosure(tester);
      await tester.tap(find.byTooltip('뒤로'));
      await tester.pumpAndSettle();
    }

    /// The first conversation row of a list (home recent or conversations).
    Finder conversationRow() => find.byType(ListTile).first;

    /// Renames the current partner through SC-07 and SC-13, then returns home.
    Future<void> rename(WidgetTester tester, String name) async {
      await tester.tap(find.text('말투 프로필'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('대화 상대 이름 수정'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), name);
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, '저장'));
      await tester.pumpAndSettle();
      await tester.pageBack();
      await tester.pumpAndSettle();
    }

    testWidgets('a recent chat reopens under the new name after a rename', (
      tester,
    ) async {
      await pumpApp(tester, factory: () => FakePersonaService());
      await nameFirstPartner(tester, '할머니');
      await chatOnce(tester);
      await rename(tester, '외할머니');
      expect(find.text('외할머니'), findsOneWidget);
      await tester.tap(conversationRow());
      await tester.pumpAndSettle();
      expect(titleIn(ChatScreen, '외할머니'), findsOneWidget);
      expect(titleIn(ChatScreen, '할머니'), findsNothing);
    });

    testWidgets('the conversations tab also uses the new name', (tester) async {
      await pumpApp(tester, factory: () => FakePersonaService());
      await nameFirstPartner(tester, '할머니');
      await chatOnce(tester);
      await rename(tester, '외할머니');
      await tester.tap(find.text('전체 보기'));
      await tester.pumpAndSettle();
      await tester.tap(conversationRow());
      await tester.pumpAndSettle();
      expect(titleIn(ChatScreen, '외할머니'), findsOneWidget);
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

  group('records belong to a partner', () {
    const father = Persona(id: 'p2', name: '아빠');

    Future<void> openRecords(WidgetTester tester) async {
      await tester.tap(find.text('기록'));
      await tester.pumpAndSettle();
    }

    Future<void> openHomeAndPick(WidgetTester tester, String name) async {
      await tester.tap(find.text('홈'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('persona-selector')));
      await tester.pumpAndSettle();
      await tester.tap(find.text(name).last);
      await tester.pumpAndSettle();
    }

    testWidgets('without a partner the records tab asks for a profile', (
      tester,
    ) async {
      final asked = <String?>[];
      await pumpApp(
        tester,
        factory: () => FakePersonaService(),
        records: (id) {
          asked.add(id);
          return PreviewRecordService();
        },
      );
      await openRecords(tester);
      expect(find.text('기록은 대화 상대별로 모여요'), findsOneWidget);
      expect(find.byType(RecordsPanel), findsNothing);
      expect(asked, isEmpty);
      await tester.tap(find.text('프로필 만들기'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsOneWidget);
    });

    testWidgets('each partner lists only its own records', (tester) async {
      final asked = <String?>[];
      await pumpApp(
        tester,
        factory: () => FakePersonaService(personas: const [mother, father]),
        records: (id) {
          asked.add(id);
          return NamedRecords('$id-record.txt');
        },
      );
      await openRecords(tester);
      expect(find.text('엄마의 기록'), findsOneWidget);
      expect(find.text('p1-record.txt'), findsOneWidget);

      await openHomeAndPick(tester, '아빠');
      await openRecords(tester);
      expect(find.text('아빠의 기록'), findsOneWidget);
      expect(find.text('p2-record.txt'), findsOneWidget);
      expect(find.text('p1-record.txt'), findsNothing);

      // Going back reuses the first partner's service.
      await openHomeAndPick(tester, '엄마');
      await openRecords(tester);
      expect(find.text('p1-record.txt'), findsOneWidget);
      expect(asked, ['p1', 'p2']);
    });

    testWidgets('SC-07 uploads go to that partner\'s records', (tester) async {
      final services = <String?, RecordService>{};
      await pumpApp(
        tester,
        factory: () => FakePersonaService(personas: const [mother, father]),
        records: (id) => services[id] = PreviewRecordService(),
      );
      await openHomeAndPick(tester, '아빠');
      await tester.tap(find.text('말투 프로필'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('기록 올리기'));
      await tester.pumpAndSettle();
      final upload = tester.widget<RecordUploadScreen>(
        find.byType(RecordUploadScreen),
      );
      final service = upload.controller.service as SessionRecordService;
      expect(service.delegate, same(services['p2']));
      expect(services.keys, ['p2']);
    });

    testWidgets('picking another partner keeps the first one\'s record state', (
      tester,
    ) async {
      await pumpApp(
        tester,
        factory: () => FakePersonaService(personas: const [mother, father]),
        records: (id) => NamedRecords('$id-record.txt'),
      );
      await openRecords(tester);
      final first = tester.widget<RecordsPanel>(find.byType(RecordsPanel));
      // An unsent transcript edit of the first partner.
      first.controller.reviewDrafts['p1-record.txt'] = const RecordReviewDraft(
        content: '합성 수정',
      );

      await openHomeAndPick(tester, '아빠');
      await openRecords(tester);
      expect(find.text('p2-record.txt'), findsOneWidget);

      await openHomeAndPick(tester, '엄마');
      await openRecords(tester);
      final again = tester.widget<RecordsPanel>(find.byType(RecordsPanel));
      expect(again.controller, same(first.controller));
      expect(again.controller.reviewDrafts['p1-record.txt']?.content, '합성 수정');
      // Still usable: a disposed controller refuses new listeners.
      expect(() => again.controller.addListener(() {}), returnsNormally);
    });

    testWidgets('only the records tab loads the records list', (tester) async {
      final services = <String?, NamedRecords>{};
      await pumpApp(
        tester,
        factory: () => FakePersonaService(personas: const [mother]),
        records: (id) => services[id] = NamedRecords('$id-record.txt'),
      );
      for (final tab in ['설정', '대화', '홈']) {
        await tester.tap(find.text(tab).last);
        await tester.pumpAndSettle();
      }
      expect(services['p1']?.listCalls ?? 0, 0);

      await openRecords(tester);
      expect(services['p1']!.listCalls, 1);
      expect(find.text('p1-record.txt'), findsOneWidget);
    });

    testWidgets('a partner made from the records tab gets its records listed', (
      tester,
    ) async {
      await pumpApp(
        tester,
        factory: () => FakePersonaService(),
        records: (id) => NamedRecords('$id-record.txt'),
      );
      await openRecords(tester);
      await tester.tap(find.text('프로필 만들기'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '엄마');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, '저장'));
      await tester.pumpAndSettle();
      expect(find.byType(SpeechProfileScreen), findsOneWidget);
      await tester.pageBack();
      await tester.pumpAndSettle();

      expect(find.byType(RecordsPanel), findsOneWidget);
      expect(find.textContaining('-record.txt'), findsOneWidget);
    });
  });
}
