import '../auth/auth_service.dart';

enum BackendFailureKind {
  unauthenticated,
  network,
  forbidden,
  staleSession,
  generic,
}

/// Frontend error contract; no HTTP client or server implementation here.
class BackendFailure implements Exception {
  const BackendFailure(
    this.kind, {
    this.code,
    this.retryable = false,
    this.stage,
  });
  const BackendFailure.unauthenticated()
    : this(BackendFailureKind.unauthenticated, code: 'UNAUTHENTICATED');
  final BackendFailureKind kind;
  final String? code;
  final bool retryable;
  final String? stage;

  /// A future transport can translate the draft API envelope at this boundary.
  /// Raw server messages are deliberately not exposed to the UI.
  factory BackendFailure.fromResponse({required int statusCode, Object? body}) {
    if (statusCode == 401) return const BackendFailure.unauthenticated();
    if (statusCode == 403) {
      return const BackendFailure(BackendFailureKind.forbidden);
    }
    final error = body is Map ? body['error'] : null;
    return BackendFailure(
      BackendFailureKind.generic,
      code: error is Map && error['code'] is String
          ? error['code'] as String
          : null,
      retryable: error is Map && error['retryable'] == true,
      stage: error is Map && ['stt', 'llm', 'tts'].contains(error['stage'])
          ? error['stage'] as String
          : null,
    );
  }
}

/// One coordinator per signed-in session. Runs an injected dummy/future request,
/// invalidates on 401, and discards results after logout or account changes.
/// It does not send requests, store data, refresh tokens, or retry operations.
class BackendService {
  BackendService(this.auth) : _generation = auth.generation;
  final AuthService auth;
  final int _generation;

  Future<T> run<T>(Future<T> Function() request) async {
    if (auth.identity == null) throw const BackendFailure.unauthenticated();
    if (_generation != auth.generation) {
      throw const BackendFailure(BackendFailureKind.staleSession);
    }
    try {
      final result = await request();
      if (_generation != auth.generation || auth.identity == null) {
        throw const BackendFailure(BackendFailureKind.staleSession);
      }
      return result;
    } on BackendFailure catch (failure) {
      if (failure.kind == BackendFailureKind.unauthenticated) {
        auth.invalidateSession(requestGeneration: _generation);
      }
      rethrow;
    }
  }
}
