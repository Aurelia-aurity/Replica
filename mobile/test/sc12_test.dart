import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/auth/auth_service.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/main.dart';
import 'package:mobile/settings/settings_screen.dart';

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
      await tester.pumpWidget(ReplicaApp(authService: auth));
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
}
