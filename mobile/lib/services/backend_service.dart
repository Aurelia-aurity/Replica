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

  bool get canRetry =>
      kind == BackendFailureKind.network ||
      (code == null ? kind != BackendFailureKind.forbidden : retryable);

  String get userMessage => switch (code) {
    'INVALID_AUDIO_DURATION' => '음성 질문은 1분 이내로 다시 녹음해 주세요.',
    'UNSUPPORTED_AUDIO_FORMAT' => '지원하지 않는 음성 형식이에요. 다른 형식으로 다시 녹음해 주세요.',
    'FILE_TOO_LARGE' => '녹음 파일이 너무 커요. 25MB 이내로 다시 녹음해 주세요.',
    'EMPTY_FILE' => '녹음된 내용이 없어요. 다시 녹음해 주세요.',
    'INVALID_INPUT' => '입력한 내용을 확인해 주세요.',
    'MESSAGE_EMPTY' => '메시지를 입력해 주세요.',
    'MESSAGE_TOO_LONG' => '질문이 너무 길어요. 더 짧게 입력해 주세요.',
    'EMPTY_TRANSCRIPT' => '음성을 인식하지 못했어요. 다시 말씀해 주세요.',
    'STT_FAILED' => '음성을 글자로 바꾸지 못했어요. 다시 시도해 주세요.',
    'TTS_FAILED' => '음성 합성에 실패했어요. 답변 글은 그대로 볼 수 있어요.',
    'AI_BUSY' => '지금 다른 요청을 처리하고 있어요. 잠시 후 다시 시도해 주세요.',
    'AI_NOT_READY' => 'AI를 준비하고 있어요. 잠시 후 다시 시도해 주세요.',
    'AI_TIMEOUT' => '답변을 만드는 데 시간이 오래 걸리고 있어요. 잠시 후 다시 시도해 주세요.',
    'AI_UNAVAILABLE' => 'AI 서버에 연결할 수 없어요. 잠시 후 다시 시도해 주세요.',
    'AI_FAILED' => '답변을 만들지 못했어요. 잠시 후 다시 시도해 주세요.',
    'CONFLICT' || 'TURN_ID_CONFLICT' => '이미 처리 중이거나 상태가 바뀌었어요. 이력을 새로고침해 주세요.',
    'NOT_FOUND' => '대화나 대화 상대를 찾을 수 없어요.',
    'NO_PERSONA' => '대화할 상대가 없어요.',
    'INTERNAL_ERROR' => '서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.',
    'UNAUTHENTICATED' => '로그인이 필요하거나 만료되었어요. 다시 로그인해 주세요.',
    _ =>
      kind == BackendFailureKind.forbidden
          ? '이 대화에 접근할 수 없어요.'
          : kind == BackendFailureKind.network
          ? '연결 상태를 확인하고 다시 시도해 주세요.'
          : '응답을 받지 못했어요.',
  };

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
