import '../services/backend_service.dart';
import 'chat_reply.dart';

export 'chat_reply.dart';

/// Basic text transport. DetailedChatService also supplies metadata and speech.
abstract class ChatService {
  bool get isDemo;
  Future<String> reply(String message);
}

abstract class DetailedChatService implements ChatService {
  Future<ChatReply> replyDetailed(String message);
  Future<SpeechAudio> synthesize(ChatReply reply);
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

class DemoChatService implements DetailedChatService {
  const DemoChatService();

  @override
  bool get isDemo => true;

  @override
  Future<String> reply(String message) async {
    return (await replyDetailed(message)).text;
  }

  @override
  Future<ChatReply> replyDetailed(String message) async {
    await Future<void>.delayed(const Duration(milliseconds: 700));
    if (message.trim() == '기록 없는 질문') {
      return const ChatReply(
        text: '기록에서 찾을 수 없어요. 관련 기록을 올리면 대화에 반영돼요.',
        evidence: EvidenceState.none,
        modelName: '화면 확인용 데모',
      );
    }
    return ChatReply(
      text: '이것은 화면 확인용 데모 응답입니다. 편하게 이야기를 이어가 보세요.',
      modelName: '화면 확인용 데모',
      evidence: EvidenceState.available,
      sources: [
        EvidenceRecord(
          fileName: '합성 예시 기록.txt',
          date: DateTime(2026, 10, 1),
          excerpt: '화면 확인용으로 만든 합성 기록입니다. 실제 사용자의 기록을 검색한 결과가 아닙니다.',
        ),
      ],
    );
  }

  @override
  Future<SpeechAudio> synthesize(ChatReply reply) async =>
      throw UnsupportedError('Demo has no synthesized voice');
}
