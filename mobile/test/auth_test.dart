import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/auth/auth_config.dart';
import 'package:mobile/auth/auth_service.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/chat/chat_service.dart';
import 'package:mobile/main.dart';

class PendingChat implements ChatService {
  final pending = Completer<String>();
  @override
  bool get isDemo => true;
  @override
  Future<String> reply(String message) => pending.future;
}

class ConfirmationAuth extends PreviewAuthService {
  @override
  bool get isPreview => false;
  final pending = Completer<bool>();
  @override
  Future<bool> signUp(String email, String password) => pending.future;
}

Future<void> login(
  WidgetTester tester, {
  String email = 'preview.a@example.com',
}) async {
  await tester.enterText(find.byKey(const ValueKey('auth-email')), email);
  await tester.enterText(
    find.byKey(const ValueKey('auth-password')),
    PreviewAuthService.password,
  );
  await tester.ensureVisible(find.byKey(const ValueKey('auth-submit')));
  await tester.tap(find.byKey(const ValueKey('auth-submit')));
  await tester.pumpAndSettle();
}

Future<void> openChat(WidgetTester tester) async {
  await tester.tap(find.text('텍스트로 대화'));
  await tester.pumpAndSettle();
  await tester.tap(find.byType(Checkbox));
  await tester.pumpAndSettle();
  await tester.tap(find.text('대화 시작'));
  await tester.pumpAndSettle();
}

void main() {
  test('preview is dev-only, missing configuration stays locked, secret keys rejected', () {
    for (final env in ['staging', 'prod']) {
      expect(
        () => AuthConfig.parse(environment: env, mode: 'preview'),
        throwsArgumentError,
      );
    }
    expect(
      AuthConfig.parse(environment: 'dev', mode: 'supabase').isConfigured,
      false,
    );
    for (final key in [
      'sb_secret_fake',
      'legacy-service-role',
      'sb_publishable_REPLACE_ME',
    ]) {
      expect(
        () => AuthConfig.parse(
          environment: 'dev',
          mode: 'supabase',
          url: 'https://test.supabase.co',
          publishableKey: key,
        ),
        throwsArgumentError,
      );
    }
    expect(
      AuthConfig.parse(
        environment: 'prod',
        mode: 'supabase',
        url: 'https://test.supabase.co',
        publishableKey: 'sb_publishable_fake',
      ).isConfigured,
      true,
    );
    expect(
      () => AuthConfig.parse(
        environment: 'dev',
        mode: 'supabase',
        url: 'http://test.supabase.co',
        publishableKey: 'sb_publishable_fake',
      ),
      throwsArgumentError,
    );
  });

  testWidgets('unauthenticated users cannot reach home, records or chat', (
    tester,
  ) async {
    final auth = UnconfiguredAuthService();
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    expect(find.text('텍스트로 대화'), findsNothing);
    expect(find.byType(NavigationBar), findsNothing);
    expect(
      tester
          .widget<FilledButton>(find.byKey(const ValueKey('auth-submit')))
          .onPressed,
      isNull,
    );
  });

  testWidgets('invalid fields and failed authentication are shown safely', (
    tester,
  ) async {
    final auth = PreviewAuthService();
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    await tester.tap(find.byKey(const ValueKey('auth-submit')));
    await tester.pumpAndSettle();
    expect(find.text('올바른 이메일을 입력해 주세요.'), findsOneWidget);
    expect(find.text('비밀번호를 입력해 주세요.'), findsOneWidget);
    await tester.enterText(
      find.byKey(const ValueKey('auth-email')),
      'wrong@example.com',
    );
    await tester.enterText(
      find.byKey(const ValueKey('auth-password')),
      'wrong',
    );
    await tester.tap(find.byKey(const ValueKey('auth-submit')));
    await tester.pumpAndSettle();
    expect(find.text('미리보기 계정과 비밀번호를 확인해 주세요.'), findsOneWidget);
    expect(auth.identity, isNull);
  });

  testWidgets(
    'signup checks password, blocks double submit and explains confirmation',
    (tester) async {
      final auth = ConfirmationAuth();
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await tester.tap(find.text('이메일로 회원가입'));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('auth-email')),
        'synthetic@example.com',
      );
      await tester.enterText(
        find.byKey(const ValueKey('auth-password')),
        'short',
      );
      await tester.enterText(
        find.byKey(const ValueKey('auth-confirmation')),
        'different',
      );
      await tester.tap(find.byKey(const ValueKey('auth-submit')));
      await tester.pumpAndSettle();
      expect(find.text('비밀번호는 8자 이상 입력해 주세요.'), findsOneWidget);
      expect(find.text('비밀번호가 일치하지 않습니다.'), findsOneWidget);
      await tester.enterText(
        find.byKey(const ValueKey('auth-password')),
        'Example123!',
      );
      await tester.enterText(
        find.byKey(const ValueKey('auth-confirmation')),
        'Example123!',
      );
      await tester.tap(find.byKey(const ValueKey('auth-submit')));
      await tester.pump();
      expect(
        tester
            .widget<FilledButton>(find.byKey(const ValueKey('auth-submit')))
            .onPressed,
        isNull,
      );
      auth.pending.complete(false);
      await tester.pumpAndSettle();
      expect(find.textContaining('이메일의 인증 링크'), findsOneWidget);
      expect(find.text('텍스트로 대화'), findsNothing);
      expect(
        tester
            .widget<TextFormField>(find.byKey(const ValueKey('auth-password')))
            .controller!
            .text,
        isEmpty,
      );
    },
  );

  testWidgets(
    'logout via account clears data and back cannot reopen previous account',
    (tester) async {
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await login(tester);
      await tester.tap(find.byTooltip('계정'));
      await tester.pumpAndSettle();
      expect(find.text('preview.a@example.com'), findsOneWidget);
      await tester.tap(find.text('로그아웃'));
      await tester.pumpAndSettle();
      expect(auth.identity, isNull);
      expect(find.text('preview.a@example.com'), findsNothing);
      await tester.binding.handlePopRoute();
      await tester.pumpAndSettle();
      expect(find.text('텍스트로 대화'), findsNothing);
      await login(tester, email: 'preview.b@example.com');
      await tester.tap(find.byTooltip('계정'));
      await tester.pumpAndSettle();
      expect(find.text('preview.b@example.com'), findsOneWidget);
    },
  );

  testWidgets(
    'expiry destroys chat draft and pending reply before switching users',
    (tester) async {
      final auth = PreviewAuthService();
      final chat = PendingChat();
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth, chatService: chat));
      await login(tester);
      await openChat(tester);
      await tester.enterText(find.byType(TextField), 'A의 합성 대화');
      await tester.tap(find.byTooltip('메시지 전송'));
      await tester.pump();
      auth.expireSession();
      await tester.pumpAndSettle();
      expect(find.textContaining('세션이 만료'), findsOneWidget);
      chat.pending.complete('A의 늦은 응답');
      await tester.pumpAndSettle();
      await login(tester, email: 'preview.b@example.com');
      await openChat(tester);
      expect(find.text('A의 합성 대화'), findsNothing);
      expect(find.text('A의 늦은 응답'), findsNothing);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        isEmpty,
      );
      await tester.enterText(find.byType(TextField), 'B의 입력 중 문장');
      await auth.signOut();
      await tester.pumpAndSettle();
      await login(tester);
      await openChat(tester);
      expect(find.text('B의 입력 중 문장'), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('expiry also dismisses the non-dismissible AI sheet', (
    tester,
  ) async {
    final auth = PreviewAuthService();
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    await login(tester);
    await tester.tap(find.text('텍스트로 대화'));
    await tester.pumpAndSettle();
    auth.expireSession();
    await tester.pumpAndSettle();
    expect(find.text('대화를 시작하기 전에'), findsNothing);
    expect(find.text('텍스트로 대화'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('auth form scrolls with small screen and keyboard', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 568);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetViewInsets);
    final auth = PreviewAuthService();
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    tester.view.viewInsets = const FakeViewPadding(bottom: 280);
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.byKey(const ValueKey('auth-submit')));
    await tester.pumpAndSettle();
    expect(
      tester.getBottomRight(find.byKey(const ValueKey('auth-submit'))).dy,
      lessThanOrEqualTo(288),
    );
    expect(tester.takeException(), isNull);
  });

  test('preview never produces a backend token', () async {
    final auth = PreviewAuthService();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    await expectLater(auth.authorizationHeaders(), throwsA(isA<AuthFailure>()));
  });
}
