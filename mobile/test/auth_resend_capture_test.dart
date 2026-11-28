import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/auth/auth_service.dart';
import 'package:mobile/main.dart';

import 'auth_resend_test.dart'
    show ResendAuth, email, rateLimitedText, sentText, signIn, signUp;

/// Renders the #125 resend states at 390x844 for the PR and the docs.
///
/// The flows run in every test run. Images are written only with
/// `--dart-define=AUTH_UI_CAPTURES=true`, which loads Malgun Gothic (as the
/// default Roboto family, so the app's own theme is used unchanged) and the
/// Material Icons font from the Windows paths below.
void main() {
  const capture = bool.fromEnvironment('AUTH_UI_CAPTURES');
  const resendKey = ValueKey('auth-resend');
  final boundary = GlobalKey();

  setUpAll(() async {
    if (!capture) return;
    final text = await File('C:/Windows/Fonts/malgun.ttf').readAsBytes();
    await (FontLoader(
      'Roboto',
    )..addFont(Future.value(ByteData.sublistView(text)))).load();
    final icons = await File(
      'C:/flutter/bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf',
    ).readAsBytes();
    await (FontLoader(
      'MaterialIcons',
    )..addFont(Future.value(ByteData.sublistView(icons)))).load();
  });

  Future<void> show(WidgetTester tester, AuthService auth) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(390, 844);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    await tester.pumpWidget(
      RepaintBoundary(
        key: boundary,
        child: ReplicaApp(authService: auth),
      ),
    );
    await tester.pumpAndSettle();
  }

  Future<void> save(WidgetTester tester, String name) async {
    expect(tester.takeException(), isNull);
    if (!capture) return;
    final render =
        boundary.currentContext!.findRenderObject()! as RenderRepaintBoundary;
    await tester.runAsync(() async {
      final image = await render.toImage(pixelRatio: 2);
      final data = await image.toByteData(format: ui.ImageByteFormat.png);
      final output = File('docs/screenshots/issue125/$name.png');
      await output.parent.create(recursive: true);
      await output.writeAsBytes(data!.buffer.asUint8List());
      image.dispose();
    });
  }

  testWidgets('capture: signup confirmation notice with resend', (
    tester,
  ) async {
    final auth = ResendAuth(
      registrationResult: SignUpResult.emailVerificationRequired,
    );
    addTearDown(auth.dispose);
    await show(tester, auth);
    await signUp(tester);
    expect(find.byKey(resendKey), findsOneWidget);
    await save(tester, 'signup-verification');
  });

  testWidgets('capture: unconfirmed sign-in, request running and outcomes', (
    tester,
  ) async {
    final auth = ResendAuth(signInFailure: AuthFailureKind.emailNotVerified);
    addTearDown(auth.dispose);
    await show(tester, auth);
    await signIn(tester);
    expect(find.text('이메일 인증이 필요해요.'), findsOneWidget);
    await save(tester, 'login-unverified');

    await tester.tap(find.byKey(resendKey));
    // The progress indicator never settles while the request is held.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text('요청 중…'), findsOneWidget);
    await save(tester, 'resend-pending');

    auth.pending.last.complete();
    await tester.pumpAndSettle();
    expect(find.text(sentText), findsOneWidget);
    await save(tester, 'resend-sent');

    await tester.tap(find.byKey(resendKey));
    await tester.pump();
    auth.pending.last.completeError(
      const AuthFailure('x', kind: AuthFailureKind.rateLimited),
    );
    await tester.pumpAndSettle();
    expect(find.text(rateLimitedText), findsOneWidget);
    expect(auth.requests, [email, email]);
    await save(tester, 'resend-rate-limited');
  });
}
