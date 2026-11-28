import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/auth/auth_service.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/main.dart';
import 'package:mobile/profile/persona_models.dart';
import 'package:mobile/profile/persona_name_screen.dart';
import 'package:mobile/profile/persona_service.dart';
import 'package:mobile/profile/speech_profile_screen.dart';
import 'package:mobile/settings/settings_screen.dart';

import 'persona_test_support.dart';

/// Mirrors SupabaseAuthService.signOut: the local identity is cleared before
/// the remote request, which then fails with a notice and an AuthFailure.
class _RemoteSignOutFailureAuth extends PreviewAuthService {
  final remote = Completer<void>();

  @override
  Future<void> signOut() async {
    updateIdentity(null);
    try {
      await remote.future;
    } catch (_) {
      notice = '이 기기에서 로그아웃했어요. 서버 로그아웃 요청은 완료하지 못했어요.';
      notifyListeners();
      throw const AuthFailure('네트워크 연결을 확인하고 다시 시도해 주세요.');
    }
  }
}

class _DatedPreviewAuth extends PreviewAuthService {
  @override
  Future<void> signIn(String email, String password) async {
    await super.signIn(email, password);
    if (email == PreviewAuthService.emails.first) {
      updateIdentity(
        AuthIdentity(id: email, email: email, createdAt: DateTime(2026, 10, 9)),
      );
    }
  }
}

/// Partners whose list is held until [gate] completes (none: answers now).
/// The list is answered once the gate opens, so a [listError] set by then is
/// the answer.
class _HeldListPersonas extends FakePersonaService {
  Completer<void>? gate = Completer<void>();

  /// Lists asked for, held ones included ([listCalls] counts answers).
  int asked = 0;

  @override
  Future<List<Persona>> list() async {
    asked++;
    final held = gate;
    if (held != null) await held.future;
    return super.list();
  }
}

/// Signs in the first preview account and shows SC-12 with [personas].
Future<void> openSettings(
  WidgetTester tester,
  FakePersonaService personas, {
  bool settle = true,
}) async {
  final auth = PreviewAuthService();
  addTearDown(auth.dispose);
  await auth.signIn(
    PreviewAuthService.emails.first,
    PreviewAuthService.password,
  );
  await tester.pumpWidget(
    ReplicaApp(authService: auth, personaServiceFactory: () => personas),
  );
  settle ? await tester.pumpAndSettle() : await tester.pump();
  await tester.tap(find.widgetWithText(NavigationDestination, '설정'));
  settle ? await tester.pumpAndSettle() : await tester.pump();
}

String shownProfileId(WidgetTester tester) => tester
    .widget<SpeechProfileScreen>(find.byType(SpeechProfileScreen))
    .personaId;

Future<void> openMenu(WidgetTester tester, String label) async {
  await tester.scrollUntilVisible(
    find.text(label),
    180,
    scrollable: find
        .descendant(
          of: find.byType(SettingsScreen),
          matching: find.byType(Scrollable),
        )
        .first,
  );
  await tester.pumpAndSettle();
  await tester.tap(find.text(label));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    'both entries select settings; notice review does not start chat',
    (tester) async {
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      await tester.pumpWidget(
        ReplicaApp(
          authService: auth,
          // Starting a chat from home needs a partner.
          personaServiceFactory: () => MemoryPersonaService(
            personas: const [Persona(id: 'p1', name: '엄마')],
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('계정'));
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
      expect(
        tester.widget<NavigationBar>(find.byType(NavigationBar)).selectedIndex,
        3,
      );
      expect(find.text(PreviewAuthService.emails.first), findsOneWidget);
      expect(find.textContaining('가입일 정보 미제공'), findsOneWidget);
      expect(find.text('11건'), findsNothing);

      await openMenu(tester, 'AI 생성 안내 다시 보기');
      expect(find.text('AI 생성 안내'), findsOneWidget);
      expect(find.byType(CheckboxListTile), findsNothing);
      expect(find.text('대화 시작'), findsNothing);
      await tester.tap(find.text('닫기'));
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
      await tester.tap(find.widgetWithText(NavigationDestination, '대화'));
      await tester.pumpAndSettle();
      expect(find.text('아직 나눈 대화가 없어요'), findsOneWidget);
      await tester.tap(find.widgetWithText(NavigationDestination, '설정'));
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
      await tester.tap(find.widgetWithText(NavigationDestination, '홈'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('텍스트로 대화'));
      await tester.pumpAndSettle();
      expect(find.byType(CheckboxListTile), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(find.widgetWithText(FilledButton, '대화 시작'))
            .onPressed,
        isNull,
      );
      await tester.tap(find.text('닫기'));
      await tester.pumpAndSettle();
    },
  );

  testWidgets('settings shows the current account creation date only', (
    tester,
  ) async {
    final auth = _DatedPreviewAuth();
    addTearDown(auth.dispose);
    final [first, second] = PreviewAuthService.emails;
    await auth.signIn(first, PreviewAuthService.password);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('계정'));
    await tester.pumpAndSettle();
    expect(find.text('이메일 계정 · 2026.10.09 가입'), findsOneWidget);

    await auth.signIn(second, PreviewAuthService.password);
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('계정'));
    await tester.pumpAndSettle();
    expect(find.text(second), findsOneWidget);
    expect(find.text('이메일 계정 · 가입일 정보 미제공'), findsOneWidget);
    expect(find.textContaining('2026.10.09'), findsNothing);
  });

  testWidgets('pending menus explain availability without simulated success', (
    tester,
  ) async {
    await tester.pumpWidget(
      const MaterialApp(home: SettingsScreen(isDemo: true)),
    );
    expect(find.text('이메일 정보 없음'), findsOneWidget);
    expect(find.text('내 기록이 쓰이는 방식'), findsNothing);
    await openMenu(tester, '기록 사용 동의 내역');
    expect(find.text('내 기록이 쓰이는 방식'), findsOneWidget);
    expect(find.text('사용 범위 안내 초안'), findsOneWidget);
    expect(find.text('동의 내역을 아직 확인할 수 없어요'), findsOneWidget);
    expect(find.textContaining('회원가입 시 개인 기록 처리 동의'), findsOneWidget);
    expect(find.text('0건'), findsNothing);
    await tester.ensureVisible(find.text('닫기'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('닫기'));
    await tester.pumpAndSettle();
    await openMenu(tester, '계정 삭제');
    expect(find.text('계정 삭제는 아직 지원하지 않아요'), findsOneWidget);
    expect(find.byType(TextField), findsNothing);
    expect(find.widgetWithText(FilledButton, '삭제'), findsNothing);
    await tester.tap(find.text('닫기'));
    await tester.pumpAndSettle();
  });

  testWidgets('account deletion row starts the provided deletion flow', (
    tester,
  ) async {
    var calls = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: SettingsScreen(
          isDemo: true,
          onDeleteAccount: () async => calls++,
        ),
      ),
    );
    await openMenu(tester, '계정 삭제');
    expect(calls, 1);
    expect(find.text('계정 삭제는 아직 지원하지 않아요'), findsNothing);
  });

  testWidgets(
    'logout cancellation, duplicate prevention and failure keep account',
    (tester) async {
      var calls = 0;
      final pending = Completer<void>();
      await tester.pumpWidget(
        MaterialApp(
          home: SettingsScreen(
            isDemo: true,
            accountEmail: 'synthetic@example.com',
            onSignOut: () {
              calls++;
              return pending.future;
            },
          ),
        ),
      );
      await openMenu(tester, '로그아웃');
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(calls, 0);
      await openMenu(tester, '로그아웃');
      await tester.tap(find.widgetWithText(FilledButton, '로그아웃'));
      await tester.pump();
      expect(calls, 1);
      expect(
        tester
            .widget<FilledButton>(find.widgetWithText(FilledButton, '로그아웃 중…'))
            .onPressed,
        isNull,
      );
      pending.completeError(StateError('synthetic failure'));
      await tester.pumpAndSettle();
      expect(find.text('로그아웃하지 못했어요. 다시 시도해 주세요.'), findsOneWidget);
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(find.text('synthetic@example.com'), findsOneWidget);
    },
  );

  testWidgets(
    'remote sign-out failure after local sign-out leaves only the login notice',
    (tester) async {
      final auth = _RemoteSignOutFailureAuth();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('계정'));
      await tester.pumpAndSettle();
      await openMenu(tester, '로그아웃');
      await tester.tap(find.widgetWithText(FilledButton, '로그아웃'));
      await tester.pump();
      // Local sign-out already removed the account tree and its dialog.
      expect(auth.identity, isNull);
      expect(find.byKey(const ValueKey('auth-email')), findsOneWidget);
      expect(find.byType(AlertDialog), findsNothing);
      auth.remote.completeError(StateError('synthetic offline'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('auth-email')), findsOneWidget);
      expect(find.byType(SettingsScreen), findsNothing);
      expect(find.byType(AlertDialog), findsNothing);
      expect(find.text('로그아웃하지 못했어요. 다시 시도해 주세요.'), findsNothing);
      expect(find.text(PreviewAuthService.emails.first), findsNothing);
      expect(
        find.text('이 기기에서 로그아웃했어요. 서버 로그아웃 요청은 완료하지 못했어요.'),
        findsOneWidget,
      );
    },
  );

  testWidgets('small screen, enlarged text and long email stay scrollable', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 568);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    await tester.pumpWidget(
      MaterialApp(
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: TextScaler.linear(1.8)),
          child: child!,
        ),
        home: SettingsScreen(
          isDemo: true,
          accountEmail: '${List.filled(70, 'a').join()}@example.com',
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    await openMenu(tester, '기록 사용 동의 내역');
    await tester.ensureVisible(find.text('닫기'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    await tester.tap(find.text('닫기'));
    await tester.pumpAndSettle();
    await openMenu(tester, '계정 삭제');
    await tester.ensureVisible(find.text('닫기'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  });

  group('speech profile menu', () {
    testWidgets('opens SC-07 of the selected partner', (tester) async {
      final personas = FakePersonaService(
        personas: const [
          Persona(id: 'p1', name: '엄마'),
          Persona(id: 'p2', name: '아빠'),
        ],
      );
      await openSettings(tester, personas);
      await tester.tap(find.widgetWithText(NavigationDestination, '홈'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('persona-selector')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('아빠').last);
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(NavigationDestination, '설정'));
      await tester.pumpAndSettle();

      await openMenu(tester, '말투 프로필');
      expect(find.byType(SpeechProfileScreen), findsOneWidget);
      expect(shownProfileId(tester), 'p2');
      expect(find.textContaining('화면 연결은 준비 중이에요'), findsNothing);
      expect(find.byType(PersonaNameScreen), findsNothing);

      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.byType(SpeechProfileScreen), findsNothing);
      expect(find.byType(SettingsScreen), findsOneWidget);
      expect(
        tester.widget<NavigationBar>(find.byType(NavigationBar)).selectedIndex,
        3,
      );
    });

    testWidgets('without a partner it names one in SC-13, then shows SC-07', (
      tester,
    ) async {
      final personas = FakePersonaService();
      await openSettings(tester, personas);
      await openMenu(tester, '말투 프로필');
      expect(find.byType(PersonaNameScreen), findsOneWidget);
      expect(find.byType(SpeechProfileScreen), findsNothing);

      // Cancelling SC-13 returns to the settings and saves nothing.
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.byType(SettingsScreen), findsOneWidget);
      expect(personas.created, isEmpty);

      await openMenu(tester, '말투 프로필');
      await tester.enterText(find.byType(TextField), '합성 상대');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, '저장'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.byType(SpeechProfileScreen), findsOneWidget);
      expect(shownProfileId(tester), personas.created.single.id);

      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
      // The new partner is now the one SC-07 opens for.
      await openMenu(tester, '말투 프로필');
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(shownProfileId(tester), personas.created.single.id);
    });

    testWidgets('while the partners load it asks to wait, opens nothing', (
      tester,
    ) async {
      final personas = _HeldListPersonas()
        ..personas.add(const Persona(id: 'p1', name: '엄마'));
      await openSettings(tester, personas, settle: false);
      await openMenu(tester, '말투 프로필');
      expect(find.text('대화 상대를 불러오는 중이에요. 잠시 후 다시 시도해 주세요.'), findsOneWidget);
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.byType(SpeechProfileScreen), findsNothing);

      personas.gate!.complete();
      await tester.pumpAndSettle();
      await openMenu(tester, '말투 프로필');
      expect(shownProfileId(tester), 'p1');
    });

    testWidgets('after a failed list it loads again, never offers a new one', (
      tester,
    ) async {
      final personas = FakePersonaService()..listError = StateError('offline');
      await openSettings(tester, personas);
      final calls = personas.listCalls;
      await openMenu(tester, '말투 프로필');
      expect(find.text('대화 상대를 불러오지 못했어요. 다시 시도해 주세요.'), findsOneWidget);
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(find.byType(SpeechProfileScreen), findsNothing);
      expect(personas.listCalls, calls + 1);

      personas
        ..listError = null
        ..personas.add(const Persona(id: 'p1', name: '엄마'));
      await openMenu(tester, '말투 프로필');
      expect(personas.listCalls, calls + 2);
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(shownProfileId(tester), 'p1');
    });

    testWidgets('taps while a failed list loads again open one screen', (
      tester,
    ) async {
      final personas = _HeldListPersonas()
        ..gate = null
        ..listError = StateError('offline');
      await openSettings(tester, personas);
      final calls = personas.asked;
      final reload = personas.gate = Completer<void>();
      personas
        ..listError = null
        ..personas.add(const Persona(id: 'p1', name: '엄마'));
      await tester.ensureVisible(find.text('말투 프로필'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('말투 프로필'));
      await tester.pump();
      await tester.tap(find.text('말투 프로필'));
      await tester.pump();
      expect(personas.asked, calls + 1);
      expect(find.text('대화 상대를 불러오는 중이에요. 잠시 후 다시 시도해 주세요.'), findsOneWidget);

      reload.complete();
      await tester.pumpAndSettle();
      expect(find.byType(SpeechProfileScreen), findsOneWidget);
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
    });

    /// SC-12 after a failed list, whose next list (with [partners], or
    /// failing with [error]) is held until the returned completer completes.
    /// "말투 프로필" is tapped once.
    Future<(_HeldListPersonas, Completer<void>)> tapDuringReload(
      WidgetTester tester, {
      List<Persona> partners = const [],
      Object? error,
    }) async {
      final personas = _HeldListPersonas()
        ..gate = null
        ..listError = StateError('offline');
      await openSettings(tester, personas);
      final reload = personas.gate = Completer<void>();
      personas
        ..listError = error
        ..personas.addAll(partners);
      await tester.ensureVisible(find.text('말투 프로필'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('말투 프로필'));
      await tester.pump();
      return (personas, reload);
    }

    /// Goes to the home tab and back to SC-12. The home tab shows a spinner
    /// while the partners load, so it is pumped for a while, not settled.
    Future<void> leaveAndComeBack(WidgetTester tester) async {
      await tester.tap(find.widgetWithText(NavigationDestination, '홈'));
      await tester.pump(const Duration(milliseconds: 500));
      expect(find.byType(SettingsScreen), findsNothing);
      await tester.tap(find.widgetWithText(NavigationDestination, '설정'));
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
    }

    testWidgets('a reload answered under the sign-out dialog opens nothing', (
      tester,
    ) async {
      final (_, reload) = await tapDuringReload(
        tester,
        partners: const [Persona(id: 'p1', name: '엄마')],
      );
      await openMenu(tester, '로그아웃');
      expect(find.widgetWithText(FilledButton, '로그아웃'), findsOneWidget);

      reload.complete();
      await tester.pumpAndSettle();
      expect(find.byType(SpeechProfileScreen), findsNothing);
      expect(find.widgetWithText(FilledButton, '로그아웃'), findsOneWidget);

      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
      expect(find.byType(SpeechProfileScreen), findsNothing);
      // The list itself was updated: the next tap opens the partner.
      await openMenu(tester, '말투 프로필');
      expect(shownProfileId(tester), 'p1');
    });

    testWidgets('a reload answered after leaving and coming back opens '
        'nothing', (tester) async {
      final (_, reload) = await tapDuringReload(
        tester,
        partners: const [Persona(id: 'p1', name: '엄마')],
      );
      await leaveAndComeBack(tester);

      reload.complete();
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
      expect(find.byType(SpeechProfileScreen), findsNothing);
      await openMenu(tester, '말투 프로필');
      expect(shownProfileId(tester), 'p1');
    });

    testWidgets('an empty reload answered after leaving opens no SC-13', (
      tester,
    ) async {
      final (personas, reload) = await tapDuringReload(tester);
      await leaveAndComeBack(tester);

      reload.complete();
      await tester.pumpAndSettle();
      expect(find.byType(SettingsScreen), findsOneWidget);
      expect(find.byType(PersonaNameScreen), findsNothing);
      expect(personas.created, isEmpty);
    });

    // Opened over SC-12 and closed again before the reload is answered.
    final covers = <String, Future<void> Function(WidgetTester)>{
      'sign-out dialog closed by 취소': (tester) async {
        await openMenu(tester, '로그아웃');
        await tester.tap(find.text('취소'));
        await tester.pumpAndSettle();
      },
      'sign-out dialog closed by system back': (tester) async {
        await openMenu(tester, '로그아웃');
        await tester.binding.handlePopRoute();
        await tester.pumpAndSettle();
      },
      'sign-out dialog closed by its barrier': (tester) async {
        await openMenu(tester, '로그아웃');
        await tester.tapAt(const Offset(4, 4));
        await tester.pumpAndSettle();
      },
      'consent sheet closed by 닫기': (tester) async {
        await openMenu(tester, '기록 사용 동의 내역');
        await tester.ensureVisible(find.text('닫기'));
        await tester.pumpAndSettle();
        await tester.tap(find.text('닫기'));
        await tester.pumpAndSettle();
      },
    };
    const failureNotice = '대화 상대를 불러오지 못했어요. 다시 시도해 주세요.';

    for (final cover in covers.entries) {
      group('after the ${cover.key}', () {
        Future<void> coverAndAnswer(
          WidgetTester tester,
          Completer<void> reload,
        ) async {
          await cover.value(tester);
          expect(find.byType(AlertDialog), findsNothing);
          expect(find.byType(BottomSheet), findsNothing);
          expect(find.byType(SettingsScreen), findsOneWidget);
          reload.complete();
          await tester.pumpAndSettle();
          expect(find.byType(SettingsScreen), findsOneWidget);
          expect(find.byType(SpeechProfileScreen), findsNothing);
          expect(find.byType(PersonaNameScreen), findsNothing);
          expect(find.text(failureNotice), findsNothing);
        }

        testWidgets('a partner answer opens no SC-07; a new tap does', (
          tester,
        ) async {
          final (_, reload) = await tapDuringReload(
            tester,
            partners: const [Persona(id: 'p1', name: '엄마')],
          );
          await coverAndAnswer(tester, reload);
          await openMenu(tester, '말투 프로필');
          expect(shownProfileId(tester), 'p1');
        });

        testWidgets('an empty answer opens no SC-13; a new tap does', (
          tester,
        ) async {
          final (personas, reload) = await tapDuringReload(tester);
          await coverAndAnswer(tester, reload);
          expect(personas.created, isEmpty);
          await openMenu(tester, '말투 프로필');
          expect(find.byType(PersonaNameScreen), findsOneWidget);
        });

        testWidgets('a failed answer shows no notice; a new tap retries', (
          tester,
        ) async {
          final (personas, reload) = await tapDuringReload(
            tester,
            error: StateError('offline'),
          );
          await coverAndAnswer(tester, reload);
          final asked = personas.asked;
          await openMenu(tester, '말투 프로필');
          expect(personas.asked, asked + 1);
          expect(find.text(failureNotice), findsOneWidget);
          expect(find.byType(PersonaNameScreen), findsNothing);
        });
      });
    }

    testWidgets('after a failed list that is truly empty it goes to SC-13', (
      tester,
    ) async {
      final personas = FakePersonaService()..listError = StateError('offline');
      await openSettings(tester, personas);
      personas.listError = null;
      await openMenu(tester, '말투 프로필');
      expect(find.byType(PersonaNameScreen), findsOneWidget);
    });
  });
}
