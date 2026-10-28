import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/auth/auth_service.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/chat/chat_service.dart';
import 'package:mobile/main.dart';
import 'package:mobile/services/backend_service.dart';

class ControlledChat implements ChatService {
  final pending = <Completer<String>>[];
  @override
  bool get isDemo => true;
  @override
  Future<String> reply(String message) {
    final request = Completer<String>();
    pending.add(request);
    return request.future;
  }
}

class RegistrationRecorder extends PreviewAuthService {
  SignUpRequest? received;
  @override
  Future<SignUpResult> performRegistration(SignUpRequest request) {
    received = request;
    return super.performRegistration(request);
  }
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
  await tester.pumpAndSettle();
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

Future<void> send(WidgetTester tester) async {
  await tester.enterText(find.byType(TextField), '합성 질문');
  await tester.pump();
  await tester.tap(find.byTooltip('메시지 전송'));
  await tester.pump();
}

void main() {
  test('registration retains consent in its request and rejects missing consent before dispatch', () async {
    final auth = RegistrationRecorder();
    addTearDown(auth.dispose);
    await expectLater(
      Future.sync(
        () => auth.register(
          const SignUpRequest(
            email: 'synthetic@example.com',
            password: 'Example123!',
            personalRecordsConsent: false,
          ),
        ),
      ),
      throwsA(
        isA<AuthFailure>().having(
          (e) => e.kind,
          'kind',
          AuthFailureKind.consentRequired,
        ),
      ),
    );
    expect(auth.received, isNull);
    const request = SignUpRequest(
      email: 'synthetic@example.com',
      password: 'Example123!',
      personalRecordsConsent: true,
    );
    expect(await auth.register(request), SignUpResult.loginRequired);
    expect(auth.received, same(request));
  });

  test('401 is not retryable even if a response body says otherwise', () {
    final error = BackendFailure.fromResponse(
      statusCode: 401,
      body: {
        'error': {
          'code': 'UNAUTHENTICATED',
          'retryable': true,
          'message': 'raw diagnostic',
        },
      },
    );
    expect(error.kind, BackendFailureKind.unauthenticated);
    expect(error.code, 'UNAUTHENTICATED');
    expect(error.retryable, false);
  });

  test('concurrent 401s invalidate only once and never retry', () async {
    final auth = PreviewAuthService();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    final backend = BackendService(auth);
    final first = Completer<String>(), second = Completer<String>();
    var notifications = 0, calls = 0;
    auth.addListener(() => notifications++);
    final checks = [
      expectLater(
        backend.run(() {
          calls++;
          return first.future;
        }),
        throwsA(isA<BackendFailure>()),
      ),
      expectLater(
        backend.run(() {
          calls++;
          return second.future;
        }),
        throwsA(isA<BackendFailure>()),
      ),
    ];
    first.completeError(const BackendFailure.unauthenticated());
    second.completeError(const BackendFailure.unauthenticated());
    await Future.wait(checks);
    expect(auth.identity, isNull);
    expect(notifications, 1);
    expect(calls, 2);
  });

  for (final nextEmail in PreviewAuthService.emails) {
    test('old 401 cannot log out a new session for $nextEmail', () async {
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      final backend = BackendService(auth);
      final pending = Completer<String>();
      final check = expectLater(
        backend.run(() => pending.future),
        throwsA(isA<BackendFailure>()),
      );
      await auth.signOut();
      await auth.signIn(nextEmail, PreviewAuthService.password);
      final generation = auth.generation;
      pending.completeError(const BackendFailure.unauthenticated());
      await check;
      expect(auth.identity!.email, nextEmail);
      expect(auth.generation, generation);
      var dispatched = false;
      await expectLater(
        backend.run(() async {
          dispatched = true;
          return 'stale';
        }),
        throwsA(
          isA<BackendFailure>().having(
            (e) => e.kind,
            'kind',
            BackendFailureKind.staleSession,
          ),
        ),
      );
      expect(dispatched, false);
    });
  }

  test('successful response from an earlier session is discarded', () async {
    final auth = PreviewAuthService();
    addTearDown(auth.dispose);
    await auth.signIn(
      PreviewAuthService.emails.first,
      PreviewAuthService.password,
    );
    final backend = BackendService(auth);
    final pending = Completer<String>();
    final check = expectLater(
      backend.run(() => pending.future),
      throwsA(
        isA<BackendFailure>().having(
          (e) => e.kind,
          'kind',
          BackendFailureKind.staleSession,
        ),
      ),
    );
    await auth.signOut();
    await auth.signIn(
      PreviewAuthService.emails.last,
      PreviewAuthService.password,
    );
    pending.complete('이전 계정의 합성 응답');
    await check;
    expect(auth.identity!.email, PreviewAuthService.emails.last);
  });

  for (final result in SignUpResult.values) {
    testWidgets('signup screen handles $result explicitly', (tester) async {
      final auth = PreviewAuthService(registrationResult: result);
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await tester.ensureVisible(find.byKey(const ValueKey('auth-switch')));
      await tester.tap(find.byKey(const ValueKey('auth-switch')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('auth-email')),
        'new.synthetic@example.com',
      );
      await tester.enterText(
        find.byKey(const ValueKey('auth-password')),
        'Example123!',
      );
      await tester.enterText(
        find.byKey(const ValueKey('auth-confirmation')),
        'Example123!',
      );
      await tester.ensureVisible(find.byKey(const ValueKey('auth-consent')));
      await tester.tap(find.byKey(const ValueKey('auth-consent')));
      await tester.ensureVisible(find.byKey(const ValueKey('auth-submit')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('auth-submit')));
      await tester.pumpAndSettle();
      switch (result) {
        case SignUpResult.signedIn:
          expect(find.text('텍스트로 대화'), findsOneWidget);
        case SignUpResult.emailVerificationRequired:
          expect(find.textContaining('이메일의 인증 링크'), findsOneWidget);
          expect(auth.identity, isNull);
        case SignUpResult.loginRequired:
          expect(find.textContaining('실제 계정 생성 없이'), findsOneWidget);
          expect(find.textContaining('이메일의 인증 링크'), findsNothing);
          expect(auth.identity, isNull);
      }
    });
  }

  for (final entry in {
    AuthFailureKind.emailNotVerified: '이메일 인증을 완료한 후 로그인해 주세요.',
    AuthFailureKind.rateLimited: '요청이 많아요. 잠시 후 다시 시도해 주세요.',
    AuthFailureKind.weakPassword: '비밀번호가 보안 정책을 충족하지 않습니다.',
  }.entries) {
    testWidgets('auth error ${entry.key} has its own safe UI message', (
      tester,
    ) async {
      final auth = PreviewAuthService(signInFailure: entry.key);
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await login(tester);
      expect(find.text(entry.value), findsOneWidget);
      expect(find.text('미리보기 인증 오류입니다.'), findsNothing);
      expect(auth.identity, isNull);
      expect(
        tester
            .widget<TextFormField>(find.byKey(const ValueKey('auth-email')))
            .controller!
            .text,
        PreviewAuthService.emails.first,
      );
    });
  }

  testWidgets(
    '401 during chat removes account navigation and re-login starts a fresh chat',
    (tester) async {
      final auth = PreviewAuthService();
      final chat = ControlledChat();
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth, chatService: chat));
      await login(tester);
      await openChat(tester);
      await send(tester);
      chat.pending.single.completeError(
        BackendFailure.fromResponse(
          statusCode: 401,
          body: {
            'error': {'code': 'UNAUTHENTICATED', 'retryable': false},
          },
        ),
      );
      await tester.pumpAndSettle();
      expect(auth.identity, isNull);
      expect(find.text('로그인이 필요하거나 만료되었어요. 다시 로그인해 주세요.'), findsOneWidget);
      expect(find.text('합성 질문'), findsNothing);
      expect(chat.pending.length, 1);
      await tester.binding.handlePopRoute();
      await tester.pumpAndSettle();
      expect(find.text('텍스트로 대화'), findsNothing);
      await login(tester, email: PreviewAuthService.emails.last);
      await openChat(tester);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        isEmpty,
      );
      expect(find.text('합성 질문'), findsNothing);
    },
  );

  testWidgets('late 401 from A leaves B chat and draft untouched', (
    tester,
  ) async {
    final auth = PreviewAuthService();
    final chat = ControlledChat();
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth, chatService: chat));
    await login(tester);
    await openChat(tester);
    await send(tester);
    await auth.signOut();
    await tester.pumpAndSettle();
    await login(tester, email: PreviewAuthService.emails.last);
    await openChat(tester);
    await tester.enterText(find.byType(TextField), 'B의 합성 초안');
    chat.pending.single.completeError(const BackendFailure.unauthenticated());
    await tester.pumpAndSettle();
    expect(auth.identity!.email, PreviewAuthService.emails.last);
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      'B의 합성 초안',
    );
    expect(tester.takeException(), isNull);
  });

  for (final failure in [
    const BackendFailure(BackendFailureKind.network, retryable: true),
    BackendFailure.fromResponse(statusCode: 403),
  ]) {
    testWidgets(
      '${failure.kind} keeps the signed-in session without automatic retry',
      (tester) async {
        final auth = PreviewAuthService();
        final chat = ControlledChat();
        addTearDown(auth.dispose);
        await tester.pumpWidget(
          ReplicaApp(authService: auth, chatService: chat),
        );
        await login(tester);
        await openChat(tester);
        await send(tester);
        chat.pending.single.completeError(failure);
        await tester.pumpAndSettle();
        expect(auth.identity, isNotNull);
        expect(find.text('합성 질문'), findsOneWidget);
        expect(chat.pending.length, 1);
      },
    );
  }
}
