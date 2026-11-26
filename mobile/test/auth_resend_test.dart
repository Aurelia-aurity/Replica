import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/auth_service.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/auth/supabase_auth_service.dart';
import 'package:mobile/main.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

const email = 'new.synthetic@example.com';
const resendKey = ValueKey('auth-resend');
const resultKey = ValueKey('auth-resend-result');
const sentText = '인증 메일을 다시 요청했어요. 메일함과 스팸메일함을 확인한 후 로그인해 주세요.';
const rateLimitedText = '인증 메일 요청이 많아요. 잠시 후 다시 시도해 주세요.';
const networkText = '네트워크 연결을 확인하고 다시 시도해 주세요.';
const failedText = '인증 메일을 다시 보내지 못했어요. 다시 시도해 주세요.';

/// Records each resend address and lets the test answer it.
class ResendAuth extends PreviewAuthService {
  ResendAuth({super.registrationResult, super.signInFailure});
  final requests = <String>[];
  final pending = <Completer<void>>[];
  @override
  bool get isPreview => false;
  @override
  Future<void> resendSignUpConfirmation(String email) {
    requests.add(email);
    final request = Completer<void>();
    pending.add(request);
    return request.future;
  }
}

Future<void> signUp(WidgetTester tester, {String address = email}) async {
  await tester.ensureVisible(find.byKey(const ValueKey('auth-switch')));
  await tester.tap(find.byKey(const ValueKey('auth-switch')));
  await tester.pumpAndSettle();
  await tester.enterText(find.byKey(const ValueKey('auth-email')), address);
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
}

Future<void> signIn(WidgetTester tester, {String address = email}) async {
  await tester.enterText(find.byKey(const ValueKey('auth-email')), address);
  await tester.enterText(
    find.byKey(const ValueKey('auth-password')),
    PreviewAuthService.password,
  );
  await tester.ensureVisible(find.byKey(const ValueKey('auth-submit')));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const ValueKey('auth-submit')));
  await tester.pumpAndSettle();
}

Future<void> tapResend(WidgetTester tester) async {
  await tester.ensureVisible(find.byKey(resendKey));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(resendKey));
  await tester.pump();
}

VoidCallback? onPressed(WidgetTester tester, Key key) =>
    tester.widget<ButtonStyleButton>(find.byKey(key)).onPressed;

String resultText(WidgetTester tester) =>
    tester.widget<Text>(find.byKey(resultKey)).data!;

SupabaseClient supabase(Future<http.Response> Function(http.Request) handler) =>
    SupabaseClient(
      'https://synthetic.supabase.co',
      'sb_publishable_synthetic',
      authOptions: const AuthClientOptions(
        autoRefreshToken: false,
        authFlowType: AuthFlowType.implicit,
      ),
      httpClient: MockClient(handler),
    );

http.Response json(Object body, int status) => http.Response(
  jsonEncode(body),
  status,
  headers: {'content-type': 'application/json'},
);

void main() {
  testWidgets(
    'signup confirmation notice resends to the signup address once at a time',
    (tester) async {
      final auth = ResendAuth(
        registrationResult: SignUpResult.emailVerificationRequired,
      );
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await signUp(tester, address: '  $email ');
      expect(find.textContaining('이메일의 인증 링크'), findsOneWidget);
      expect(find.text('인증 메일 다시 보내기'), findsOneWidget);

      await tapResend(tester);
      expect(auth.requests, [email]);
      expect(onPressed(tester, resendKey), isNull);
      expect(onPressed(tester, const ValueKey('auth-submit')), isNull);
      expect(onPressed(tester, const ValueKey('auth-switch')), isNull);
      expect(find.text('인증 메일 요청 중…'), findsOneWidget);
      await tester.tap(find.byKey(resendKey), warnIfMissed: false);
      await tester.pump();
      expect(auth.requests, hasLength(1));

      auth.pending.single.complete();
      await tester.pumpAndSettle();
      expect(resultText(tester), sentText);
      expect(onPressed(tester, resendKey), isNotNull);
      expect(find.textContaining('이메일의 인증 링크'), findsOneWidget);
    },
  );

  testWidgets(
    'unconfirmed sign-in offers resend, explains failures and allows retry',
    (tester) async {
      final auth = ResendAuth(signInFailure: AuthFailureKind.emailNotVerified);
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await signIn(tester);
      expect(find.text('이메일 인증을 완료한 후 로그인해 주세요.'), findsOneWidget);

      for (final (failure, message) in [
        (
          const AuthFailure('x', kind: AuthFailureKind.rateLimited),
          rateLimitedText,
        ),
        (const AuthFailure('x', kind: AuthFailureKind.network), networkText),
        (const AuthFailure('private diagnostic'), failedText),
        (StateError('unexpected'), failedText),
      ]) {
        await tapResend(tester);
        expect(onPressed(tester, resendKey), isNull);
        auth.pending.last.completeError(failure);
        await tester.pumpAndSettle();
        expect(resultText(tester), message);
        expect(find.textContaining('private diagnostic'), findsNothing);
        expect(onPressed(tester, resendKey), isNotNull);
      }

      await tapResend(tester);
      expect(find.byKey(resultKey), findsNothing);
      auth.pending.last.complete();
      await tester.pumpAndSettle();
      expect(resultText(tester), sentText);
      expect(auth.requests, List.filled(5, email));
    },
  );

  testWidgets(
    'resend is offered only for the address that still needs confirmation',
    (tester) async {
      final auth = ResendAuth(
        signInFailure: AuthFailureKind.invalidCredentials,
      );
      addTearDown(auth.dispose);
      await tester.pumpWidget(ReplicaApp(authService: auth));
      await signIn(tester);
      expect(find.byKey(resendKey), findsNothing);

      final unconfirmed = ResendAuth(
        signInFailure: AuthFailureKind.emailNotVerified,
      );
      addTearDown(unconfirmed.dispose);
      await tester.pumpWidget(ReplicaApp(authService: unconfirmed));
      await tester.pumpAndSettle();
      await signIn(tester);
      expect(find.byKey(resendKey), findsOneWidget);
      await tester.enterText(
        find.byKey(const ValueKey('auth-email')),
        'other.synthetic@example.com',
      );
      await tester.pump();
      expect(find.byKey(resendKey), findsNothing);

      await signIn(tester);
      expect(find.byKey(resendKey), findsOneWidget);
      await tester.ensureVisible(find.byKey(const ValueKey('auth-switch')));
      await tester.tap(find.byKey(const ValueKey('auth-switch')));
      await tester.pumpAndSettle();
      expect(find.byKey(resendKey), findsNothing);
      expect(unconfirmed.requests, isEmpty);
    },
  );

  testWidgets('signup that needs no confirmation offers no resend', (
    tester,
  ) async {
    final auth = ResendAuth(registrationResult: SignUpResult.loginRequired);
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    await signUp(tester);
    expect(find.text('회원가입이 완료되었어요. 로그인해 주세요.'), findsOneWidget);
    expect(find.byKey(resendKey), findsNothing);
  });

  testWidgets('preview resend says no email was sent', (tester) async {
    final auth = PreviewAuthService(
      signInFailure: AuthFailureKind.emailNotVerified,
    );
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    await signIn(tester);
    await tapResend(tester);
    await tester.pumpAndSettle();
    expect(resultText(tester), contains('실제 메일을 보내지 않아요'));
  });

  testWidgets('preview resend failure keeps the button for another try', (
    tester,
  ) async {
    final auth = PreviewAuthService(
      signInFailure: AuthFailureKind.emailNotVerified,
      resendFailure: AuthFailureKind.rateLimited,
    );
    addTearDown(auth.dispose);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    await signIn(tester);
    await tapResend(tester);
    await tester.pumpAndSettle();
    expect(resultText(tester), rateLimitedText);
    expect(onPressed(tester, resendKey), isNotNull);
  });

  test('SDK resend asks for the signup email of the given address', () async {
    final requests = <http.Request>[];
    final client = supabase((request) async {
      requests.add(request);
      return json(<String, Object>{}, 200);
    });
    final auth = SupabaseAuthService(
      client,
      emailRedirectTo: 'com.replica://auth-callback',
    );
    addTearDown(() async {
      auth.dispose();
      await client.dispose();
    });
    await auth.resendSignUpConfirmation(email);
    final request = requests.single;
    expect(request.method, 'POST');
    expect(request.url.path, endsWith('/auth/v1/resend'));
    expect(
      request.url.queryParameters['redirect_to'],
      'com.replica://auth-callback',
    );
    final body = jsonDecode(request.body) as Map<String, Object?>;
    expect(body['email'], email);
    expect(body['type'], 'signup');
    expect(auth.identity, isNull);
  });

  for (final (name, response, kind) in [
    (
      'email send limit',
      json({'error_code': 'over_email_send_rate_limit', 'msg': 'secret'}, 429),
      AuthFailureKind.rateLimited,
    ),
    (
      'request limit',
      json({'error_code': 'over_request_rate_limit', 'msg': 'secret'}, 429),
      AuthFailureKind.rateLimited,
    ),
    ('bare 429', json({'msg': 'secret'}, 429), AuthFailureKind.rateLimited),
    (
      'unknown rejection',
      json({'error_code': 'synthetic_unknown', 'msg': 'secret'}, 400),
      AuthFailureKind.generic,
    ),
    // The SDK treats 5xx as retryable fetch errors, as for sign-in.
    ('server error', json({'msg': 'secret'}, 500), AuthFailureKind.network),
  ]) {
    test('SDK resend maps $name without raw server details', () async {
      final client = supabase((_) async => response);
      final auth = SupabaseAuthService(client);
      addTearDown(() async {
        auth.dispose();
        await client.dispose();
      });
      await expectLater(
        auth.resendSignUpConfirmation(email),
        throwsA(
          isA<AuthFailure>()
              .having((error) => error.kind, 'kind', kind)
              .having((error) => error.message, 'message', isNot('secret')),
        ),
      );
    });
  }

  test('SDK resend reports a lost connection as a network failure', () async {
    final client = supabase(
      (_) async => throw http.ClientException('synthetic offline'),
    );
    final auth = SupabaseAuthService(client);
    addTearDown(() async {
      auth.dispose();
      await client.dispose();
    });
    await expectLater(
      auth.resendSignUpConfirmation(email),
      throwsA(
        isA<AuthFailure>().having(
          (error) => error.kind,
          'kind',
          AuthFailureKind.network,
        ),
      ),
    );
  });
}
