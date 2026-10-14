import 'dart:async';

import 'package:flutter/foundation.dart';

enum AuthFailureKind {
  generic,
  invalidCredentials,
  network,
  emailAlreadyRegistered,
}

class AuthFailure implements Exception {
  const AuthFailure(this.message, {this.kind = AuthFailureKind.generic});
  final String message;
  final AuthFailureKind kind;
}

class AuthIdentity {
  const AuthIdentity({required this.id, required this.email});
  final String id;
  final String email;
}

/// The UI depends on this interface, never on tokens or Supabase SDK types.
abstract class AuthService extends ChangeNotifier {
  AuthIdentity? _identity;
  Timer? _expiry;
  DateTime? _expiresAt;
  int _generation = 0;
  String? notice;

  AuthIdentity? get identity => _identity;
  int get generation => _generation;
  bool get isPreview;
  bool get isConfigured => true;

  Future<void> signIn(String email, String password);

  /// False means email confirmation is required before signing in.
  Future<bool> signUp(String email, String password);

  /// Integration point for registration and consent. No record storage here.
  Future<bool> register({
    required String email,
    required String password,
    required bool personalRecordsConsent,
  }) {
    if (!personalRecordsConsent) {
      throw const AuthFailure('개인 기록 처리 동의가 필요합니다.');
    }
    return signUp(email, password);
  }

  Future<void> signOut();

  /// Future backend clients must call this for each request, not cache tokens.
  Future<Map<String, String>> authorizationHeaders();

  void checkSessionExpiry() {
    if (_identity != null &&
        _expiresAt != null &&
        !_expiresAt!.isAfter(DateTime.now())) {
      notice = '세션이 만료되었어요. 다시 로그인해 주세요.';
      updateIdentity(null);
    }
  }

  @protected
  void updateIdentity(AuthIdentity? next, {DateTime? expiresAt}) {
    _expiry?.cancel();
    _expiresAt = expiresAt;
    if (expiresAt != null && !expiresAt.isAfter(DateTime.now())) next = null;
    if (_identity?.id != next?.id) _generation++;
    _identity = next;
    if (next != null && expiresAt != null) {
      _expiry = Timer(expiresAt.difference(DateTime.now()), () {
        notice = '세션이 만료되었어요. 다시 로그인해 주세요.';
        updateIdentity(null);
      });
    }
    notifyListeners();
  }

  @override
  void dispose() {
    _expiry?.cancel();
    super.dispose();
  }
}

class UnconfiguredAuthService extends AuthService {
  @override
  bool get isConfigured => false;
  @override
  bool get isPreview => false;
  @override
  Future<void> signIn(String email, String password) async =>
      throw const AuthFailure('인증 환경 설정이 필요합니다.');
  @override
  Future<bool> signUp(String email, String password) async =>
      throw const AuthFailure('인증 환경 설정이 필요합니다.');
  @override
  Future<void> signOut() async => updateIdentity(null);
  @override
  Future<Map<String, String>> authorizationHeaders() async =>
      throw const AuthFailure('로그인이 필요합니다.');
}
