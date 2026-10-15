import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/auth/auth_service.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/main.dart';

class NetworkAuth extends PreviewAuthService {
  int calls = 0;
  @override
  Future<void> signIn(String email, String password) async {
    if (++calls == 1) {
      throw const AuthFailure(
        'synthetic network failure',
        kind: AuthFailureKind.network,
      );
    }
    return super.signIn(email, password);
  }
}

class ConsentAuth extends PreviewAuthService {
  bool? receivedConsent;
  @override
  Future<bool> register({
    required String email,
    required String password,
    required bool personalRecordsConsent,
  }) async {
    receivedConsent = personalRecordsConsent;
    return super.register(
      email: email,
      password: password,
      personalRecordsConsent: personalRecordsConsent,
    );
  }
}

Future<void> fill(
  WidgetTester tester, {
  String email = 'preview.a@example.com',
}) async {
  await tester.enterText(find.byKey(const ValueKey('auth-email')), email);
  await tester.enterText(
    find.byKey(const ValueKey('auth-password')),
    PreviewAuthService.password,
  );
  await tester.pump();
}

Future<void> submit(WidgetTester tester) async {
  await tester.ensureVisible(find.byKey(const ValueKey('auth-submit')));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const ValueKey('auth-submit')));
  await tester.pumpAndSettle();
}

void main() {
  for (final size in [
    const Size(320, 568),
    const Size(360, 640),
    const Size(400, 860),
  ]) {
    testWidgets('login fits without scrolling at $size', (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = size;
      tester.view.padding = const FakeViewPadding(top: 24, bottom: 24);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetPadding);
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await tester.pumpAndSettle();
      expect(find.text('미리보기 계정 안내'), findsNothing);
      expect(
        tester
            .state<ScrollableState>(find.byType(Scrollable).first)
            .position
            .maxScrollExtent,
        lessThanOrEqualTo(.5),
      );
      expect(
        tester.getBottomRight(find.text('올린 기록과 대화는 내 계정에서만 볼 수 있어요.')).dy,
        lessThanOrEqualTo(size.height - 24),
      );
      expect(tester.takeException(), isNull);
      final upperGap =
          tester.getTopLeft(find.text('이메일')).dy -
          tester.getBottomRight(find.text('남겨둔 기록으로 다시 나누는 대화')).dy;
      final lowerGap =
          tester.getTopLeft(find.byKey(const ValueKey('auth-privacy'))).dy -
          tester.getBottomRight(find.byType(Wrap)).dy;
      expect(upperGap / lowerGap, closeTo(2 / 3, .015));
    });
  }
  testWidgets(
    'SC-01 header, privacy footer, button gating and password visibility',
    (tester) async {
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      expect(find.text('레플리카'), findsOneWidget);
      expect(find.text('남겨둔 기록으로 다시 나누는 대화'), findsOneWidget);
      expect(find.text('올린 기록과 대화는 내 계정에서만 볼 수 있어요.'), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(find.byKey(const ValueKey('auth-submit')))
            .onPressed,
        isNull,
      );
      await tester.enterText(
        find.byKey(const ValueKey('auth-email')),
        'preview.a@example.com',
      );
      await tester.pump();
      expect(
        tester
            .widget<FilledButton>(find.byKey(const ValueKey('auth-submit')))
            .onPressed,
        isNull,
      );
      await tester.enterText(
        find.byKey(const ValueKey('auth-password')),
        PreviewAuthService.password,
      );
      await tester.pump();
      expect(
        tester
            .widget<FilledButton>(find.byKey(const ValueKey('auth-submit')))
            .onPressed,
        isNotNull,
      );
      expect(
        tester
            .widget<EditableText>(
              find.descendant(
                of: find.byKey(const ValueKey('auth-password')),
                matching: find.byType(EditableText),
              ),
            )
            .obscureText,
        true,
      );
      await tester.tap(find.byTooltip('비밀번호 보기'));
      await tester.pump();
      expect(
        tester
            .widget<EditableText>(
              find.descendant(
                of: find.byKey(const ValueKey('auth-password')),
                matching: find.byType(EditableText),
              ),
            )
            .obscureText,
        false,
      );
    },
  );

  testWidgets('signup requires consent and passes it to the frontend service', (
    tester,
  ) async {
    final auth = ConsentAuth();
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    await tester.ensureVisible(find.byKey(const ValueKey('auth-switch')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('auth-switch')));
    await tester.pumpAndSettle();
    await fill(tester, email: 'new.synthetic@example.com');
    await tester.enterText(
      find.byKey(const ValueKey('auth-confirmation')),
      PreviewAuthService.password,
    );
    await tester.pump();
    expect(
      tester
          .widget<FilledButton>(find.byKey(const ValueKey('auth-submit')))
          .onPressed,
      isNull,
    );
    expect(auth.receivedConsent, isNull);
    await tester.ensureVisible(find.byKey(const ValueKey('auth-consent')));
    await tester.tap(find.byKey(const ValueKey('auth-consent')));
    await submit(tester);
    expect(auth.receivedConsent, true);
    expect(find.textContaining('실제 계정 생성 없이'), findsOneWidget);
    expect(auth.identity, isNull);
  });

  testWidgets(
    'registered email shows inline error and switches to login keeping email',
    (tester) async {
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await tester.ensureVisible(find.byKey(const ValueKey('auth-switch')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('auth-switch')));
      await tester.pumpAndSettle();
      await fill(tester);
      await tester.enterText(
        find.byKey(const ValueKey('auth-confirmation')),
        PreviewAuthService.password,
      );
      await tester.ensureVisible(find.byKey(const ValueKey('auth-consent')));
      await tester.tap(find.byKey(const ValueKey('auth-consent')));
      await submit(tester);
      expect(find.text('이미 가입된 이메일이에요. 로그인해 주세요.'), findsOneWidget);
      await tester.ensureVisible(find.text('로그인으로 전환'));
      await tester.tap(find.text('로그인으로 전환'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('auth-confirmation')), findsNothing);
      expect(
        tester
            .widget<TextFormField>(find.byKey(const ValueKey('auth-email')))
            .controller!
            .text,
        'preview.a@example.com',
      );
    },
  );

  testWidgets('network failure offers toast and retry while preserving input', (
    tester,
  ) async {
    final auth = NetworkAuth();
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    await fill(tester);
    await submit(tester);
    expect(find.text('네트워크 연결을 확인하고 다시 시도해 주세요.'), findsOneWidget);
    expect(
      tester
          .widget<TextFormField>(find.byKey(const ValueKey('auth-password')))
          .controller!
          .text,
      PreviewAuthService.password,
    );
    await tester.ensureVisible(find.byKey(const ValueKey('auth-retry')));
    await tester.tap(find.byKey(const ValueKey('auth-retry')));
    await tester.pumpAndSettle();
    expect(auth.calls, 2);
    expect(find.text('텍스트로 대화'), findsOneWidget);
  });

  testWidgets('signup scrolls at 320px with keyboard and large text', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 568);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetViewInsets);
    final auth = PreviewAuthService();
    addTearDown(auth.dispose);
    await tester.pumpWidget(
      MaterialApp(
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: const TextScaler.linear(1.4)),
          child: child!,
        ),
        home: ReplicaApp(authService: auth),
      ),
    );
    await tester.ensureVisible(find.byKey(const ValueKey('auth-switch')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('auth-switch')));
    await tester.pumpAndSettle();
    tester.view.viewInsets = const FakeViewPadding(bottom: 280);
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.byKey(const ValueKey('auth-consent')));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    await tester.ensureVisible(find.byKey(const ValueKey('auth-submit')));
    await tester.pumpAndSettle();
    expect(
      tester.getBottomRight(find.byKey(const ValueKey('auth-submit'))).dy,
      lessThanOrEqualTo(288),
    );
  });
}
