import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/chat/chat_screen.dart';
import 'package:mobile/chat/chat_service.dart';
import 'package:mobile/main.dart';
import 'package:mobile/profile/persona_controller.dart';
import 'package:mobile/profile/persona_models.dart';
import 'package:mobile/profile/persona_name_screen.dart';
import 'package:mobile/profile/persona_service.dart';
import 'package:mobile/profile/speech_profile_screen.dart';

import 'persona_test_support.dart';

const mother = Persona(id: 'p1', name: '엄마');

Future<PersonaController> loaded(FakePersonaService service) async {
  final controller = PersonaController(service);
  addTearDown(controller.dispose);
  await controller.load();
  return controller;
}

Future<void> pumpProfile(
  WidgetTester tester,
  PersonaController controller, {
  RecordUploadLauncher? launcher,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: SpeechProfileScreen(
        controller: controller,
        onOpenRecordUpload: launcher,
      ),
    ),
  );
  await tester.pumpAndSettle();
}

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

Future<void> startTextChat(WidgetTester tester) async {
  await tester.tap(find.text('텍스트로 대화'));
  await tester.pumpAndSettle();
  await tester.tap(find.byType(Checkbox));
  await tester.pumpAndSettle();
  await tester.tap(find.text('대화 시작'));
  await tester.pumpAndSettle();
}

void main() {
  group('SC-07 states', () {
    testWidgets('shows loading, then the profile', (tester) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(),
      );
      final controller = await loaded(service);
      service.profileGate = Completer<void>();
      await tester.pumpWidget(
        MaterialApp(home: SpeechProfileScreen(controller: controller)),
      );
      await tester.pump();
      await tester.pump();
      expect(find.bySemanticsLabel('말투 프로필 불러오는 중'), findsOneWidget);
      expect(find.text('엄마의 말투'), findsOneWidget);
      expect(find.text('프로필 다시 만들기'), findsNothing);
      service.profileGate!.complete();
      await tester.pumpAndSettle();
      expect(find.text('자주 쓰는 표현'), findsOneWidget);
    });

    testWidgets('shows the name, summary and the three item groups', (
      tester,
    ) async {
      final now = DateTime.now();
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(),
      );
      await pumpProfile(tester, await loaded(service));
      expect(find.text('엄마의 말투'), findsOneWidget);
      expect(
        find.text('확정된 기록 11개에서 뽑았어요 · ${now.month}월 ${now.day}일 갱신'),
        findsOneWidget,
      );
      for (final title in ['자주 쓰는 표현', '호칭', '문장 끝맺음']) {
        expect(find.text(title), findsOneWidget);
      }
      for (final item in ['밥은 먹었나', '걱정이다', '우리 딸', '니', '~나?', '~데이']) {
        expect(find.text(item), findsOneWidget);
      }
      // Item exclusion is only proposed in the screen plan, so no delete UI.
      expect(find.byIcon(Icons.close), findsNothing);
      expect(find.text('미리보기 데이터예요. 서버에서 받은 말투 프로필이 아니에요.'), findsNothing);
    });

    testWidgets('without a record count only the date is shown', (
      tester,
    ) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(count: null),
      );
      await pumpProfile(tester, await loaded(service));
      expect(find.textContaining('확정된 기록'), findsNothing);
      expect(find.textContaining('갱신'), findsOneWidget);
    });

    testWidgets('preview data is labelled as such', (tester) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(),
        preview: true,
      );
      await pumpProfile(tester, await loaded(service));
      expect(find.textContaining('미리보기 데이터예요'), findsOneWidget);
    });

    testWidgets('empty groups say so instead of showing a blank card', (
      tester,
    ) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(
          expressions: const [],
          addressTerms: const [],
          endings: const [],
        ),
      );
      await pumpProfile(tester, await loaded(service));
      expect(find.text('추출된 항목이 없어요'), findsNWidgets(3));
    });

    testWidgets('no profile explains it and leads to record upload', (
      tester,
    ) async {
      final service = FakePersonaService(personas: const [mother]);
      Persona? uploadFor;
      await pumpProfile(
        tester,
        await loaded(service),
        launcher: (context, persona) async => uploadFor = persona,
      );
      expect(find.text('말투를 파악하려면 기록이 더 필요해요'), findsOneWidget);
      expect(find.text('자주 쓰는 표현'), findsNothing);
      expect(find.text('프로필 다시 만들기'), findsNothing);
      expect(find.text('대화 상대 이름 수정'), findsOneWidget);
      await tester.tap(find.text('기록 올리기'));
      await tester.pumpAndSettle();
      expect(uploadFor, mother);
    });

    testWidgets('record upload not connected yet says so', (tester) async {
      final service = FakePersonaService(personas: const [mother]);
      await pumpProfile(tester, await loaded(service));
      await tester.tap(find.text('기록 올리기'));
      await tester.pumpAndSettle();
      expect(find.text('기록 올리기는 아직 연결되지 않았어요.'), findsOneWidget);
    });

    testWidgets('a failed lookup can be retried', (tester) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(),
      );
      final controller = await loaded(service);
      service.profileError = StateError('offline');
      await pumpProfile(tester, controller);
      expect(find.text('말투 프로필을 불러오지 못했어요.'), findsOneWidget);
      expect(find.text('자주 쓰는 표현'), findsNothing);
      service.profileError = null;
      await tester.tap(find.text('다시 시도'));
      await tester.pumpAndSettle();
      expect(find.text('자주 쓰는 표현'), findsOneWidget);
    });
  });

  group('SC-07 layout', () {
    testWidgets('many items, a very long item and a long name fit 320 wide', (
      tester,
    ) async {
      smallScreen(tester);
      final service = FakePersonaService(
        personas: [Persona(id: 'p1', name: '가' * 20)],
        profileValue: sampleProfile(
          expressions: [
            for (var i = 0; i < 16; i++) '자주 쓰는 표현 번호 $i 입니다',
            '길게 이어지는 표현 ' * 40,
          ],
          addressTerms: [for (var i = 0; i < 16; i++) '호칭$i'],
          endings: [for (var i = 0; i < 16; i++) '~어미$i?'],
        ),
      );
      await pumpProfile(tester, await loaded(service));
      expect(tester.takeException(), isNull);
      await tester.scrollUntilVisible(find.text('프로필 다시 만들기'), 400);
      expect(tester.takeException(), isNull);
      expect(find.text('대화 상대 이름 수정'), findsOneWidget);
    });

    testWidgets('the no-profile state fits a small screen', (tester) async {
      smallScreen(tester);
      final service = FakePersonaService(
        personas: [Persona(id: 'p1', name: '가' * 20)],
      );
      await pumpProfile(tester, await loaded(service));
      expect(tester.takeException(), isNull);
      expect(find.text('기록 올리기'), findsOneWidget);
    });
  });

  group('SC-07 rename', () {
    testWidgets('edits the name in SC-13 and returns with it applied', (
      tester,
    ) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(),
      );
      await pumpProfile(tester, await loaded(service));
      await tester.tap(find.text('대화 상대 이름 수정'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsOneWidget);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        '엄마',
      );
      await tester.enterText(find.byType(TextField), '어머니');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, '저장'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.text('어머니의 말투'), findsOneWidget);
      expect(find.text('대화 상대 이름을 저장했어요.'), findsOneWidget);
      expect(find.text('걱정이다'), findsOneWidget); // items kept
    });

    testWidgets('cancelling keeps the old name', (tester) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(),
      );
      await pumpProfile(tester, await loaded(service));
      await tester.tap(find.text('대화 상대 이름 수정'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '다른 이름');
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(find.text('엄마의 말투'), findsOneWidget);
      expect(service.renames, isEmpty);
    });
  });

  group('SC-07 regenerate', () {
    final button = find.widgetWithText(FilledButton, '프로필 다시 만들기');

    testWidgets('shows progress, keeps the old items, then the result', (
      tester,
    ) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(expressions: const ['이전 표현']),
      );
      await pumpProfile(tester, await loaded(service));
      service.regenerateGate = Completer<void>();
      await tester.scrollUntilVisible(button, 200);
      await tester.tap(button);
      await tester.pump();
      expect(find.text('다시 만드는 중…'), findsOneWidget);
      expect(find.text('대화는 이전 프로필로 계속할 수 있어요.'), findsOneWidget);
      expect(find.text('이전 표현'), findsOneWidget);
      await tester.tap(find.byType(FilledButton), warnIfMissed: false);
      await tester.pump();
      expect(service.regenerateCalls, 1);

      service.regenerateGate!.complete();
      await tester.pumpAndSettle();
      expect(find.text('새 프로필로 바꿨어요.'), findsOneWidget);
      expect(find.text('새 표현'), findsOneWidget);
      expect(find.text('이전 표현'), findsNothing);
    });

    testWidgets('a failure keeps the previous profile and allows another try', (
      tester,
    ) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(expressions: const ['이전 표현']),
      )..regenerateError = StateError('model busy');
      await pumpProfile(tester, await loaded(service));
      await tester.scrollUntilVisible(button, 200);
      await tester.tap(button);
      await tester.pumpAndSettle();
      expect(find.textContaining('프로필을 다시 만들지 못했어요'), findsOneWidget);
      expect(find.text('이전 표현'), findsOneWidget);
      expect(tester.widget<FilledButton>(button).onPressed, isNotNull);

      service.regenerateError = null;
      await tester.tap(button);
      await tester.pumpAndSettle();
      expect(find.text('새 프로필로 바꿨어요.'), findsOneWidget);
      expect(find.textContaining('프로필을 다시 만들지 못했어요'), findsNothing);
    });

    testWidgets('without a server API the button is disabled and says so', (
      tester,
    ) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(),
        supported: {},
      );
      await pumpProfile(tester, await loaded(service));
      await tester.scrollUntilVisible(find.textContaining('(미연동)'), 200);
      expect(tester.widget<FilledButton>(button).onPressed, isNull);
      expect(find.textContaining('(미연동)'), findsOneWidget);
      expect(service.regenerateCalls, 0);
    });
  });

  group('home flow', () {
    testWidgets('preview partner card shows its status and opens SC-07', (
      tester,
    ) async {
      await pumpApp(tester);
      expect(find.text('엄마'), findsOneWidget);
      expect(find.text('말투 프로필 준비됨 · 기록 11개 · 미리보기'), findsOneWidget);
      await tester.tap(find.text('엄마'));
      await tester.pumpAndSettle();
      expect(find.byType(SpeechProfileScreen), findsOneWidget);
      expect(find.text('엄마의 말투'), findsOneWidget);
      expect(find.textContaining('미리보기 데이터예요'), findsOneWidget);
      expect(find.text('확정된 기록 11개에서 뽑았어요 · 10월 6일 갱신'), findsOneWidget);
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.byType(SpeechProfileScreen), findsNothing);
      expect(find.text('텍스트로 대화'), findsOneWidget);
    });

    testWidgets('a finished regeneration banner is gone on the next visit', (
      tester,
    ) async {
      await pumpApp(tester);
      await tester.tap(find.text('엄마'));
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(find.text('프로필 다시 만들기'), 200);
      await tester.tap(find.text('프로필 다시 만들기'));
      await tester.pump();
      expect(find.text('다시 만드는 중…'), findsOneWidget);
      await tester.pump(const Duration(milliseconds: 1300));
      await tester.pumpAndSettle();
      expect(find.text('새 프로필로 바꿨어요.'), findsOneWidget);
      await tester.pageBack();
      await tester.pumpAndSettle();
      await tester.tap(find.text('엄마'));
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(find.text('프로필 다시 만들기'), 200);
      expect(find.text('새 프로필로 바꿨어요.'), findsNothing);
    });

    testWidgets('no partner: SC-13, then SC-07 with the no-profile state', (
      tester,
    ) async {
      final service = FakePersonaService();
      await pumpApp(tester, factory: () => service);
      expect(find.text('아직 대화 상대가 없어요'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsNothing);
      await tester.tap(find.text('말투 프로필 만들기'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsOneWidget);
      await tester.enterText(find.byType(TextField), '할머니');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, '저장'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.text('할머니의 말투'), findsOneWidget);
      expect(find.text('말투를 파악하려면 기록이 더 필요해요'), findsOneWidget);
      expect(service.created.single.name, '할머니');

      // The partner stays on home although no analysis result exists yet.
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.text('할머니'), findsOneWidget);
      expect(find.text('말투 프로필이 아직 없어요'), findsOneWidget);
      expect(find.text('아직 대화 상대가 없어요'), findsNothing);

      // A second tap opens SC-07 and does not create another partner.
      await tester.tap(find.text('할머니'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.text('할머니의 말투'), findsOneWidget);
      expect(service.created, hasLength(1));
    });

    testWidgets('cancelling SC-13 from home leaves no partner behind', (
      tester,
    ) async {
      final service = FakePersonaService();
      await pumpApp(tester, factory: () => service);
      await tester.tap(find.text('말투 프로필 만들기'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '할머니');
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(find.text('아직 대화 상대가 없어요'), findsOneWidget);
      expect(find.byType(SpeechProfileScreen), findsNothing);
      expect(service.created, isEmpty);
    });

    testWidgets('an existing partner without a profile skips SC-13', (
      tester,
    ) async {
      final service = FakePersonaService(personas: const [mother]);
      await pumpApp(tester, factory: () => service);
      expect(find.text('말투 프로필이 아직 없어요'), findsOneWidget);
      await tester.tap(find.text('엄마'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.text('말투를 파악하려면 기록이 더 필요해요'), findsOneWidget);
      expect(service.created, isEmpty);
    });

    testWidgets('the saved name reaches home and the conversation screen', (
      tester,
    ) async {
      final service = FakePersonaService(
        personas: const [mother],
        profileValue: sampleProfile(),
      );
      await pumpApp(tester, factory: () => service);
      await tester.tap(find.text('엄마'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('대화 상대 이름 수정'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '어머니');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, '저장'));
      await tester.pumpAndSettle();
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.text('어머니'), findsOneWidget);
      expect(find.text('엄마'), findsNothing);
      await startTextChat(tester);
      expect(find.byType(ChatScreen), findsOneWidget);
      expect(
        find.descendant(
          of: find.byType(ChatScreen),
          matching: find.text('어머니'),
        ),
        findsWidgets,
      );
    });

    testWidgets('a failed partner list can be retried from the card', (
      tester,
    ) async {
      final service = FakePersonaService(personas: const [mother])
        ..listError = StateError('offline');
      await pumpApp(tester, factory: () => service);
      expect(find.text('대화 상대를 불러오지 못했어요.'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsNothing);
      service.listError = null;
      await tester.tap(find.text('다시 시도'));
      await tester.pumpAndSettle();
      expect(find.text('엄마'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsOneWidget);
    });

    testWidgets('a failed profile lookup shows a status instead of guessing', (
      tester,
    ) async {
      final service = FakePersonaService(personas: const [mother])
        ..profileError = StateError('offline');
      await pumpApp(tester, factory: () => service);
      expect(find.text('말투 프로필 상태를 확인하지 못했어요'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsOneWidget);
    });

    testWidgets('home card fits 320 wide with the longest name', (
      tester,
    ) async {
      smallScreen(tester);
      final service = FakePersonaService(
        personas: [Persona(id: 'p1', name: '가' * 20)],
        profileValue: sampleProfile(),
      );
      await pumpApp(tester, factory: () => service);
      expect(tester.takeException(), isNull);
      expect(find.text('텍스트로 대화'), findsOneWidget);
    });
  });
}
