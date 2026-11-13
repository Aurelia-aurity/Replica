import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/main.dart' as app;
import 'package:mobile/auth/auth_config.dart';
import 'package:mobile/auth/auth_service.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/config/app_config.dart';

void main() {
  test('email callback accepts an app deep link and requires HTTPS for web in prod', () {
    AuthConfig config(String redirect) => AuthConfig.parse(
      environment: 'prod',
      mode: 'supabase',
      url: 'https://project.supabase.co',
      publishableKey: 'sb_publishable_test',
      emailRedirectTo: redirect,
    );

    expect(
      config('com.replica://auth-callback').emailRedirectTo,
      'com.replica://auth-callback',
    );
    expect(config('https://app.example.com/auth').isConfigured, isTrue);
    expect(() => config('http://app.example.com/auth'), throwsArgumentError);
    expect(
      () => config('https://user@app.example.com/auth'),
      throwsArgumentError,
    );
  });

  for (final env in ['dev', 'staging', 'prod']) {
    for (final mode in ['preview', 'supabase']) {
      test('service boundary $env/$mode', () async {
        final service = await app.selectAuthService(
          AppConfig.parse(
            environment: env,
            backendBaseUrl: 'https://api.example.com',
          ),
          AuthConfig(mode: mode),
        );
        addTearDown(service.dispose);
        expect(
          service is PreviewAuthService,
          env == 'dev' && mode == 'preview',
        );
        if (env != 'dev' || mode != 'preview') {
          expect(service, isA<UnconfiguredAuthService>());
        }
      });
    }
  }

  testWidgets(
    'entry point respects build environment and cannot unlock protected home',
    (tester) async {
      const env = String.fromEnvironment('APP_ENV', defaultValue: 'dev');
      const mode = String.fromEnvironment(
        'AUTH_MODE',
        defaultValue: 'supabase',
      );
      if (env != 'dev' && mode == 'preview') {
        await expectLater(app.main(), throwsArgumentError);
        expect(find.byType(app.ReplicaApp), findsNothing);
        return;
      }
      await app.main();
      await tester.pumpAndSettle();
      final auth = tester
          .widget<app.ReplicaApp>(find.byType(app.ReplicaApp))
          .authService;
      expect(auth is PreviewAuthService, env == 'dev' && mode == 'preview');
      await tester.enterText(
        find.byKey(const ValueKey('auth-email')),
        PreviewAuthService.emails.first,
      );
      await tester.enterText(
        find.byKey(const ValueKey('auth-password')),
        PreviewAuthService.password,
      );
      await tester.ensureVisible(find.byKey(const ValueKey('auth-submit')));
      await tester.pumpAndSettle();
      if (env == 'dev' && mode == 'preview') {
        await tester.tap(find.byKey(const ValueKey('auth-submit')));
        await tester.pumpAndSettle();
        expect(find.text('텍스트로 대화'), findsOneWidget);
      } else {
        expect(auth, isA<UnconfiguredAuthService>());
        expect(
          tester
              .widget<FilledButton>(find.byKey(const ValueKey('auth-submit')))
              .onPressed,
          isNull,
        );
        expect(find.text('텍스트로 대화'), findsNothing);
        expect(auth.identity, isNull);
      }
      await tester.pumpWidget(const SizedBox());
      auth.dispose();
    },
  );
}
