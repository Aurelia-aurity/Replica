import 'auth_service.dart';

/// Synthetic accounts for frontend development only. Never sends a request.
class PreviewAuthService extends AuthService {
  static const password = 'Replica123!';
  static const emails = ['preview.a@example.com', 'preview.b@example.com'];

  @override
  bool get isPreview => true;

  @override
  Future<void> signIn(String email, String password) async {
    if (!emails.contains(email) || password != PreviewAuthService.password) {
      throw const AuthFailure('미리보기 계정과 비밀번호를 확인해 주세요.');
    }
    notice = null;
    updateIdentity(AuthIdentity(id: email, email: email));
  }

  @override
  Future<bool> signUp(String email, String password) async => false;

  @override
  Future<void> signOut() async => updateIdentity(null);

  void expireSession() {
    notice = '세션이 만료되었어요. 다시 로그인해 주세요.';
    updateIdentity(null);
  }

  @override
  Future<Map<String, String>> authorizationHeaders() async =>
      throw const AuthFailure('미리보기 모드에서는 서버 인증을 제공하지 않습니다.');
}
