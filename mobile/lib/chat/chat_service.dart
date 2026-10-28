import '../services/backend_service.dart';

/// Replace this implementation when the backend API contract is agreed.
abstract class ChatService {
  bool get isDemo;
  Future<String> reply(String message);
}

/// Keeps the existing screen independent of authentication and transport.
class SessionChatService implements ChatService {
  SessionChatService({required this.backend, required this.delegate});
  final BackendService backend;
  final ChatService delegate;
  @override
  bool get isDemo => delegate.isDemo;
  @override
  Future<String> reply(String message) =>
      backend.run(() => delegate.reply(message));
}

class DemoChatService implements ChatService {
  const DemoChatService();

  @override
  bool get isDemo => true;

  @override
  Future<String> reply(String message) async {
    await Future<void>.delayed(const Duration(milliseconds: 700));
    return '이것은 화면 확인용 데모 응답입니다. 편하게 이야기를 이어가 보세요.';
  }
}
