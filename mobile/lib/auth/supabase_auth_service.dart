import 'dart:async';

import 'package:supabase_flutter/supabase_flutter.dart';

import 'auth_service.dart';

class SupabaseAuthService extends AuthService {
  SupabaseAuthService(this._client, {this.emailRedirectTo, super.now}) {
    _accept(_client.auth.currentSession);
    _subscription = _client.auth.onAuthStateChange.listen(
      (state) {
        if (state.event == AuthChangeEvent.signedOut &&
            state.signOutReason == SignOutReason.userInitiated &&
            !state.fromBroadcast) {
          _finishLocalSignOut();
        }
        _accept(state.session);
      },
      onError: (Object error, StackTrace stack) {
        notice = switch (error) {
          AuthException(code: 'otp_expired') => '이메일 인증 링크가 만료되었거나 이미 사용되었어요.',
          AuthRetryableFetchException() => '인증 요청을 완료하지 못했어요. 연결을 확인해 주세요.',
          _ => '인증을 완료하지 못했어요. 다시 로그인해 주세요.',
        };
        _accept(_client.auth.currentSession);
      },
    );
  }

  final SupabaseClient _client;
  final String? emailRedirectTo;
  late final StreamSubscription<AuthState> _subscription;
  bool _acceptSessions = true;
  Completer<void>? _localSignOut;
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
      await _localSignOut?.future;
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
      await _localSignOut?.future;
      _acceptSessions = true;
      notice = null;
      final response = await _client.auth.signUp(
        email: request.email,
        password: request.password,
        emailRedirectTo: emailRedirectTo,
        data: {'personal_records_consent': request.personalRecordsConsent},
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
    if (invalidated) {
      _acceptSessions = false;
      // Clear the SDK session too, so a restart cannot restore a rejected token.
      _clearSdkSession().ignore();
    }
    return invalidated;
  }

  @override
  Future<void> signOut() async {
    _acceptSessions = false;
    // Remove the entire account navigation tree before waiting on the network.
    updateIdentity(null);
    try {
      await _clearSdkSession();
    } catch (error) {
      if (!_acceptSessions) {
        notice = '이 기기에서 로그아웃했어요. 서버 로그아웃 요청은 완료하지 못했어요.';
        notifyListeners();
      }
      throw _failure(error);
    }
  }

  Future<void> _clearSdkSession() async {
    final pending = _localSignOut;
    if (pending != null) return pending.future;
    final localSignOut = _localSignOut = Completer<void>();
    try {
      // The SDK emits signedOut after clearing its local session and before
      // awaiting the remote logout request.
      await _client.auth.signOut(scope: SignOutScope.local);
    } finally {
      if (identical(_localSignOut, localSignOut)) _finishLocalSignOut();
    }
  }

  void _finishLocalSignOut() {
    _localSignOut?.complete();
    _localSignOut = null;
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
