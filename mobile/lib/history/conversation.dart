import 'dart:math';

import '../chat/chat_reply.dart';

String newConversationId() {
  final random = Random.secure();
  final bytes = List.generate(16, (_) => random.nextInt(256));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}

class ConversationTurn {
  const ConversationTurn({
    required this.id,
    required this.number,
    required this.createdAt,
    this.question,
    this.answer,
    this.voice = false,
    this.questionStatus = 'done',
    this.answerStatus = 'done',
    this.replyDetails,
  });
  final String id;
  final int number;
  final DateTime createdAt;
  final String? question, answer;
  final bool voice;
  final String questionStatus;
  final String? answerStatus;
  final ChatReply? replyDetails;

  factory ConversationTurn.fromJson(
    Map<String, dynamic> json, {
    ChatReply? replyDetails,
  }) {
    if (json['answer'] != null && json['is_ai_generated'] != true) {
      throw const FormatException('Missing AI disclosure');
    }
    return ConversationTurn(
      id: json['turn_id'] as String,
      number: json['turn_no'] as int,
      createdAt: DateTime.parse(json['created_at'] as String),
      question: json['question'] as String?,
      answer: json['answer'] as String?,
      voice: json['input_mode'] == 'voice',
      questionStatus: json['question_status'] as String,
      answerStatus: json['answer_status'] as String?,
      replyDetails:
          replyDetails ??
          (json['answer'] is String
              ? ChatReply(
                  text: json['answer'] as String,
                  turnId: json['turn_id'] as String,
                  modelName: json['model_name'] as String?,
                  timings: {
                    for (final key in [
                      'stt_ms',
                      'llm_ms',
                      'tts_ms',
                      'total_ms',
                    ])
                      key: json[key] is int ? json[key] as int : null,
                  },
                )
              : null),
    );
  }

  bool get pending => questionStatus == 'pending' || answerStatus == 'pending';
}

class Conversation {
  Conversation({
    required this.id,
    required this.personaId,
    required this.createdAt,
    required this.updatedAt,
    required this.acknowledgedAt,
    this.title,
    this.initialVoice = false,
    List<ConversationTurn> turns = const [],
  }) : turns = List.unmodifiable(
         [...turns]..sort((a, b) => a.number.compareTo(b.number)),
       );
  final String id, personaId;
  final String? title;
  final bool initialVoice;
  final DateTime createdAt, updatedAt, acknowledgedAt;
  final List<ConversationTurn> turns;

  factory Conversation.fromJson(
    Map<String, dynamic> json,
    List<ConversationTurn> turns, {
    bool initialVoice = false,
  }) => Conversation(
    id: json['id'] as String,
    personaId: json['persona_id'] as String,
    title: json['title'] as String?,
    initialVoice: initialVoice,
    createdAt: DateTime.parse(json['created_at'] as String),
    updatedAt: DateTime.parse(json['updated_at'] as String),
    acknowledgedAt: DateTime.parse(json['ai_notice_ack_at'] as String),
    turns: turns,
  );

  String get firstQuestion {
    for (final turn in turns) {
      if (turn.question?.trim().isNotEmpty == true) return turn.question!;
    }
    return title?.trim().isNotEmpty == true ? title! : '새 대화';
  }

  DateTime get lastAt => turns.fold(
    updatedAt,
    (last, turn) => turn.createdAt.isAfter(last) ? turn.createdAt : last,
  );
  bool get lastVoice => turns.isEmpty ? initialVoice : turns.last.voice;
  int get messageCount => turns.fold(
    0,
    (count, turn) =>
        count +
        (turn.question?.isNotEmpty == true ? 1 : 0) +
        (turn.answer?.isNotEmpty == true ? 1 : 0),
  );
  bool get pending => turns.any((turn) => turn.pending);
  bool get hasFailedTurns => turns.any(
    (turn) => turn.questionStatus == 'failed' || turn.answerStatus == 'failed',
  );

  List<({String text, bool isUser})> get messages => [
    for (final turn in turns) ...[
      if (turn.question?.isNotEmpty == true)
        (text: turn.question!, isUser: true),
      if (turn.answer?.isNotEmpty == true) (text: turn.answer!, isUser: false),
    ],
  ];

  Map<int, ChatReply> get replyMetadata {
    final result = <int, ChatReply>{};
    var index = 0;
    for (final turn in turns) {
      if (turn.question?.isNotEmpty == true) index++;
      if (turn.answer?.isNotEmpty == true) {
        result[index++] =
            turn.replyDetails ?? ChatReply(text: turn.answer!, turnId: turn.id);
      }
    }
    return result;
  }
}

String conversationDateGroup(DateTime value, DateTime now) {
  final date = value.toLocal();
  final today = DateTime(now.year, now.month, now.day);
  final day = DateTime(date.year, date.month, date.day);
  if (day == today) return '오늘';
  final monday = today.subtract(Duration(days: today.weekday - 1));
  if (!day.isBefore(monday) && day.isBefore(today)) return '이번 주';
  if (date.year == now.year && date.month == now.month) return '이번 달';
  return date.year == now.year
      ? '${date.month}월'
      : '${date.year}년 ${date.month}월';
}

String conversationTime(DateTime value) {
  final date = value.toLocal();
  final hour = date.hour % 12 == 0 ? 12 : date.hour % 12;
  return '${date.month}월 ${date.day}일 · ${date.hour < 12 ? '오전' : '오후'} $hour:${date.minute.toString().padLeft(2, '0')}';
}
