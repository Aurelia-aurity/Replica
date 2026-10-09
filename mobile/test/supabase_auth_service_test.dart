import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/auth_service.dart';
import 'package:mobile/auth/supabase_auth_service.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

const userId = '00000000-0000-4000-8000-000000000001';
Map<String, Object?> sessionResponse({int expiresIn = 3600}) {
  final exp = DateTime.now().millisecondsSinceEpoch ~/ 1000 + expiresIn;
  String part(Object value) =>
      base64Url.encode(utf8.encode(jsonEncode(value))).replaceAll('=', '');
  return {
    'access_token':
        '${part({'alg': 'HS256', 'typ': 'JWT'})}.${part({'sub': userId, 'exp': exp})}.synthetic',
    'token_type': 'bearer',
    'expires_in': expiresIn,
    'expires_at': exp,
    'refresh_token': 'synthetic-refresh-token',
    'user': {
      'id': userId,
      'email': 'synthetic@example.com',
      'aud': 'authenticated',
      'app_metadata': <String, Object>{},
      'user_metadata': <String, Object>{},
      'created_at': '2026-10-09T00:00:00Z',
    },
  };
}

void main() {
  test('SDK adapter signs in, exposes current bearer token and clears logout locally on network error', () async {
    final response = sessionResponse();
    final requests = <http.Request>[];
    final client = SupabaseClient(
      'https://synthetic.supabase.co',
      'sb_publishable_synthetic',
      authOptions: const AuthClientOptions(autoRefreshToken: false),
      httpClient: MockClient((request) async {
        requests.add(request);
        if (request.url.path.endsWith('/logout')) {
          throw http.ClientException('synthetic offline');
        }
        return http.Response(
          jsonEncode(response),
          200,
          headers: {'content-type': 'application/json'},
        );
      }),
    );
    final auth = SupabaseAuthService(client);
    addTearDown(() async {
      auth.dispose();
      await client.dispose();
    });
    await auth.signIn('synthetic@example.com', 'Example123!');
    expect(auth.identity!.id, userId);
    expect(jsonDecode(requests.first.body)['email'], 'synthetic@example.com');
    expect(requests.first.url.queryParameters['grant_type'], 'password');
    expect(
      (await auth.authorizationHeaders())['Authorization'],
      'Bearer ${response['access_token']}',
    );
    final logout = auth.signOut();
    expect(auth.identity, isNull);
    await expectLater(logout, throwsA(isA<AuthFailure>()));
    expect(client.auth.currentSession, isNull);
    await expectLater(auth.authorizationHeaders(), throwsA(isA<AuthFailure>()));
  });

  test('SDK confirmation-only signup does not grant access', () async {
    final client = SupabaseClient(
      'https://synthetic.supabase.co',
      'sb_publishable_synthetic',
      authOptions: const AuthClientOptions(
        autoRefreshToken: false,
        authFlowType: AuthFlowType.implicit,
      ),
      httpClient: MockClient(
        (_) async => http.Response(
          jsonEncode(sessionResponse()['user']),
          200,
          headers: {'content-type': 'application/json'},
        ),
      ),
    );
    final auth = SupabaseAuthService(client);
    addTearDown(() async {
      auth.dispose();
      await client.dispose();
    });
    expect(await auth.signUp('synthetic@example.com', 'Example123!'), false);
    expect(auth.identity, isNull);
  });

  test(
    'SDK invalid credentials are translated without raw server details',
    () async {
      final client = SupabaseClient(
        'https://synthetic.supabase.co',
        'sb_publishable_synthetic',
        authOptions: const AuthClientOptions(autoRefreshToken: false),
        httpClient: MockClient(
          (_) async => http.Response(
            jsonEncode({
              'error_code': 'invalid_credentials',
              'msg': 'private server diagnostic',
            }),
            400,
            headers: {'content-type': 'application/json'},
          ),
        ),
      );
      final auth = SupabaseAuthService(client);
      addTearDown(() async {
        auth.dispose();
        await client.dispose();
      });
      await expectLater(
        auth.signIn('synthetic@example.com', 'wrong'),
        throwsA(
          isA<AuthFailure>().having(
            (error) => error.message,
            'message',
            '이메일 또는 비밀번호를 확인해 주세요.',
          ),
        ),
      );
      expect(auth.identity, isNull);
    },
  );

  test('SDK session expiry clears identity without backend access', () async {
    final client = SupabaseClient(
      'https://synthetic.supabase.co',
      'sb_publishable_synthetic',
      authOptions: const AuthClientOptions(autoRefreshToken: false),
      httpClient: MockClient(
        (_) async => http.Response(
          jsonEncode(sessionResponse(expiresIn: 5)),
          200,
          headers: {'content-type': 'application/json'},
        ),
      ),
    );
    final auth = SupabaseAuthService(client);
    await auth.signIn('synthetic@example.com', 'Example123!');
    expect(auth.identity, isNotNull);
    await Future<void>.delayed(const Duration(seconds: 6));
    expect(auth.identity, isNull);
    expect(auth.notice, contains('세션이 만료'));
    auth.dispose();
    await client.dispose();
  });
}
