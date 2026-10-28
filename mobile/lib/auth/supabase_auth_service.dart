import 'dart:async';

import 'package:supabase_flutter/supabase_flutter.dart';

import 'auth_service.dart';

class SupabaseAuthService extends AuthService {
  SupabaseAuthService(this._client, {super.now}) {
    _accept(_client.auth.currentSession);
    _subscription = _client.auth.onAuthStateChange.listen(
      (state) {
        if (!_loggingOut) _accept(state.session);
      },
      onError: (Object error, StackTrace stack) {
        // Offline refresh must not crash or expose raw server errors.
        notice = '세션을 갱신하지 못했어요. 연결을 확인해 주세요.';
        _accept(_client.auth.currentSession);
      },
    );
  }

  final SupabaseClient _client;
  late final StreamSubscription<AuthState> _subscription;
  bool _loggingOut = false;
  bool _acceptSessions = true;
  @override
  bool get isPreview => false;

  void _accept(Session? session) {
    if (!_acceptSessions && session != null) return;
    updateIdentity(
      session == null
          ? null
          : AuthIdentity(id: session.user.id, email: session.user.email ?? ''),
      expiresAt: session?.expiresAt == null
          ? null
          : DateTime.fromMillisecondsSinceEpoch(session!.expiresAt! * 1000),
    );
  }

  AuthFailure _failure(Object error) {
    if (error is AuthRetryableFetchException || error is TimeoutException) {
      return const AuthFailure(
        '네트워크 연결을 확인하고 다시 시도해 주세요.',
        kind: AuthFailureKind.network,
      );
    }
    if (error is AuthException) {
      switch (error.code) {
        case 'invalid_credentials':
          return const AuthFailure(
            '이메일 또는 비밀번호를 확인해 주세요.',
            kind: AuthFailureKind.invalidCredentials,
          );
        case 'email_not_confirmed':
          return const AuthFailure(
            '이메일 인증을 완료한 후 로그인해 주세요.',
            kind: AuthFailureKind.emailNotVerified,
          );
        case 'weak_password':
          return const AuthFailure(
            '비밀번호가 서버 보안 정책을 충족하지 않습니다.',
            kind: AuthFailureKind.weakPassword,
          );
        case 'user_already_exists':
        case 'email_exists':
          return const AuthFailure(
            '이미 가입된 이메일이에요. 로그인해 주세요.',
            kind: AuthFailureKind.emailAlreadyRegistered,
          );
        case 'over_request_rate_limit':
        case 'over_email_send_rate_limit':
          return const AuthFailure(
            '요청이 많아요. 잠시 후 다시 시도해 주세요.',
            kind: AuthFailureKind.rateLimited,
          );
      }
    }
    return const AuthFailure('인증 요청을 완료하지 못했어요. 연결을 확인하고 다시 시도해 주세요.');
  }

  @override
  Future<void> signIn(String email, String password) async {
    try {
      _acceptSessions = true;
      notice = null;
      final response = await _client.auth.signInWithPassword(
        email: email,
        password: password,
      );
      _accept(response.session);
    } catch (error) {
      throw _failure(error);
    }
  }

  @override
  Future<SignUpResult> performRegistration(SignUpRequest request) async {
    try {
      _acceptSessions = true;
      notice = null;
      final response = await _client.auth.signUp(
        email: request.email,
        password: request.password,
      );
      _accept(response.session);
      return response.session != null
          ? SignUpResult.signedIn
          : SignUpResult.emailVerificationRequired;
    } catch (error) {
      throw _failure(error);
    }
  }

  @override
  bool invalidateSession({required int requestGeneration}) {
    final invalidated = super.invalidateSession(
      requestGeneration: requestGeneration,
    );
    if (invalidated) _acceptSessions = false;
    return invalidated;
  }

  @override
  Future<void> signOut() async {
    _loggingOut = true;
    _acceptSessions = false;
    // Remove the entire account navigation tree before waiting on the network.
    updateIdentity(null);
    try {
      await _client.auth.signOut(scope: SignOutScope.local);
    } catch (error) {
      notice = '화면 데이터는 지웠지만 로그아웃 요청을 완료하지 못했어요. 다시 시도해 주세요.';
      notifyListeners();
      throw _failure(error);
    } finally {
      _loggingOut = false;
    }
  }

  @override
  Future<Map<String, String>> authorizationHeaders() async {
    final userId = identity?.id;
    final currentGeneration = generation;
    if (userId == null) throw const AuthFailure('로그인이 필요합니다.');
    var session = _client.auth.currentSession;
    if (session != null &&
        (session.expiresAt ?? 0) <=
            DateTime.now().millisecondsSinceEpoch ~/ 1000 + 30) {
      try {
        session = (await _client.auth.refreshSession()).session;
      } catch (error) {
        throw _failure(error);
      }
    }
    if (generation != currentGeneration ||
        identity?.id != userId ||
        session == null ||
        session.user.id != userId ||
        session.isExpired) {
      throw const AuthFailure('세션이 만료되었어요. 다시 로그인해 주세요.');
    }
    return {'Authorization': 'Bearer ${session.accessToken}'};
  }

  @override
  void dispose() {
    unawaited(_subscription.cancel());
    super.dispose();
  }
}
