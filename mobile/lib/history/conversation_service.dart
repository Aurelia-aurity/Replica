import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:http_parser/http_parser.dart';

import '../chat/chat_service.dart';
import '../services/backend_service.dart';
import 'conversation.dart';

abstract class ConversationService {
  bool get isDemo;
  Future<List<Conversation>> list();
  Future<Conversation> get(String id);
  Future<Conversation> create(DateTime acknowledgedAt, {bool voice = false});
  Future<String> send(String id, String turnId, String message);
  Future<void> delete(String id);
}

abstract class DetailedConversationService implements ConversationService {
  Future<ChatReply> sendDetailed(String id, String turnId, String message);
  Future<SpeechAudio> synthesize(ChatReply reply);
}

abstract class VoiceConversationService implements DetailedConversationService {
  Future<VoiceReply> sendVoice(
    String id,
    String turnId,
    VoiceRecording recording,
  );
}

/// Owned by one login generation, never shared between accounts or persisted.
class MemoryConversationService implements VoiceConversationService {
  MemoryConversationService({required this.backend, required this.chat});
  final BackendService backend;
  final ChatService chat;
  final _items = <String, Conversation>{};
  final _voiceReplies = <String, VoiceReply>{};
  @override
  bool get isDemo => true;
  @override
  Future<List<Conversation>> list() =>
      backend.run(() async => _items.values.toList());
  @override
  Future<Conversation> get(String id) => backend.run(() async => _items[id]!);
  @override
  Future<Conversation> create(DateTime acknowledgedAt, {bool voice = false}) =>
      backend.run(() async {
        final now = DateTime.now();
        final item = Conversation(
          id: newConversationId(),
          personaId: 'preview',
          createdAt: now,
          updatedAt: now,
          acknowledgedAt: acknowledgedAt,
          initialVoice: voice,
        );
        _items[item.id] = item;
        return item;
      });
  @override
  Future<String> send(String id, String turnId, String message) async =>
      (await sendDetailed(id, turnId, message)).text;

  @override
  Future<ChatReply> sendDetailed(String id, String turnId, String message) =>
      backend.run(() async {
        final before = _items[id]!;
        for (final turn in before.turns) {
          if (turn.id == turnId) {
            return turn.replyDetails ??
                ChatReply(text: turn.answer!, turnId: turnId);
          }
        }
        final detail = (await backend.run(
          () async => chat is DetailedChatService
              ? await (chat as DetailedChatService).replyDetailed(message)
              : ChatReply(text: await chat.reply(message)),
        )).withTurnId(turnId);
        if (detail.text.trim().isEmpty) {
          throw const FormatException('Empty answer');
        }
        final now = DateTime.now();
        _items[id] = Conversation(
          id: id,
          personaId: before.personaId,
          createdAt: before.createdAt,
          updatedAt: now,
          acknowledgedAt: before.acknowledgedAt,
          initialVoice: before.initialVoice,
          turns: [
            ...before.turns,
            ConversationTurn(
              id: turnId,
              number: before.turns.length + 1,
              createdAt: now,
              question: message,
              answer: detail.text,
              replyDetails: detail,
            ),
          ],
        );
        return detail;
      });
  @override
  Future<SpeechAudio> synthesize(ChatReply reply) => backend.run(() async {
    if (chat is DetailedChatService) {
      return (chat as DetailedChatService).synthesize(reply);
    }
    throw UnsupportedError('No speech service');
  });
  @override
  Future<VoiceReply> sendVoice(
    String id,
    String turnId,
    VoiceRecording recording,
  ) => backend.run(() async {
    recording.validate();
    if (_voiceReplies.containsKey(turnId)) return _voiceReplies[turnId]!;
    final before = _items[id]!;
    const transcript = '음성 대화 화면 확인용 합성 질문입니다.';
    final reply = await sendDetailed(id, turnId, transcript);
    final current = _items[id]!;
    _items[id] = Conversation(
      id: id,
      personaId: current.personaId,
      createdAt: before.createdAt,
      updatedAt: current.updatedAt,
      acknowledgedAt: before.acknowledgedAt,
      initialVoice: before.initialVoice,
      turns: [
        for (final turn in current.turns)
          if (turn.id == turnId)
            ConversationTurn(
              id: turn.id,
              number: turn.number,
              createdAt: turn.createdAt,
              question: turn.question,
              answer: turn.answer,
              replyDetails: turn.replyDetails,
              voice: true,
            )
          else
            turn,
      ],
    );
    return _voiceReplies[turnId] = VoiceReply(
      transcript: transcript,
      reply: reply,
      preview: true,
    );
  });
  @override
  Future<void> delete(String id) => backend.run(() async {
    for (final turn in _items[id]?.turns ?? <ConversationTurn>[]) {
      _voiceReplies.remove(turn.id);
    }
    _items.remove(id);
  });
}

/// Adapter for the supplied draft API. No preview credentials are sent here.
class HttpConversationService implements VoiceConversationService {
  HttpConversationService({
    required this.backend,
    required this.baseUrl,
    required this.client,
  });
  final BackendService backend;
  final Uri baseUrl;
  final http.Client client;
  // The draft session API has no initial input mode field. Keep it for this login.
  final _initialVoiceModes = <String, bool>{};
  final _replyDetails = <String, Map<String, ChatReply>>{};
  @override
  bool get isDemo => false;

  Uri _uri(String path, [Map<String, String>? query]) => baseUrl.replace(
    path: '${baseUrl.path.replaceFirst(RegExp(r'/$'), '')}$path',
    queryParameters: query,
  );

  Future<Object?> _request(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, Object?>? body,
  }) {
    final request = http.Request(method, _uri(path, query))
      ..headers['Content-Type'] = 'application/json';
    if (body != null) request.body = jsonEncode(body);
    return _perform(request);
  }

  Future<Object?> _perform(http.BaseRequest request) => backend.run(() async {
    final headers = await backend.auth.authorizationHeaders();
    // Check generation again after asynchronous token acquisition.
    return backend.run(() async {
      request.headers.addAll(headers);
      final response = await (() async => http.Response.fromStream(
        await client.send(request),
      ))().timeout(const Duration(seconds: 30));
      Object? json;
      if (response.bodyBytes.isNotEmpty) {
        try {
          json = jsonDecode(utf8.decode(response.bodyBytes));
        } catch (_) {
          if (response.statusCode >= 200 && response.statusCode < 300) rethrow;
        }
      }
      if (response.statusCode < 200 || response.statusCode >= 300) {
        throw BackendFailure.fromResponse(
          statusCode: response.statusCode,
          body: json,
        );
      }
      if (request.method == 'DELETE' && response.statusCode != 204) {
        throw const FormatException('Deletion not confirmed');
      }
      return json;
    });
  });

  Future<List<ConversationTurn>> _turns(String id) async {
    final turns = <ConversationTurn>[];
    var after = 0;
    while (true) {
      final json = await _request(
        'GET',
        '/sessions/${Uri.encodeComponent(id)}/turns',
        query: {'limit': '100', 'after_turn_no': '$after'},
      ) as Map<String, dynamic>;
      final items = (json['items'] as List).cast<Map<String, dynamic>>();
      final limit = json['limit'] as int;
      if (limit < 1) throw const FormatException('Invalid limit');
      final page = items
          .map(
            (json) => ConversationTurn.fromJson(
              json,
              replyDetails: _replyDetails[id]?[json['turn_id']],
            ),
          )
          .toList();
      if (page.any((turn) => turn.number <= after)) {
        throw const FormatException('Invalid turn cursor');
      }
      turns.addAll(page);
      if (items.length < limit) break;
      after = page.map((turn) => turn.number).reduce((a, b) => a > b ? a : b);
    }
    return turns;
  }

  @override
  Future<List<Conversation>> list() async {
    final sessions = <Map<String, dynamic>>[];
    var offset = 0;
    while (true) {
      final json = await _request(
        'GET',
        '/sessions',
        query: {'limit': '50', 'offset': '$offset'},
      ) as Map<String, dynamic>;
      final page = (json['items'] as List).cast<Map<String, dynamic>>();
      final limit = json['limit'] as int;
      if (limit < 1) throw const FormatException('Invalid limit');
      sessions.addAll(page);
      if (page.length < limit) break;
      offset += page.length;
    }
    final result = <Conversation>[];
    // Bound concurrency while deriving metadata missing from session objects.
    for (var i = 0; i < sessions.length; i += 4) {
      result.addAll(
        await Future.wait(
          sessions
              .skip(i)
              .take(4)
              .map(
                (json) async => Conversation.fromJson(
                  json,
                  await _turns(json['id'] as String),
                  initialVoice: _initialVoiceModes[json['id']] ?? false,
                ),
              ),
        ),
      );
    }
    return result;
  }

  @override
  Future<Conversation> get(String id) async {
    final json = await _request(
      'GET',
      '/sessions/${Uri.encodeComponent(id)}',
    ) as Map<String, dynamic>;
    return Conversation.fromJson(
      json,
      await _turns(id),
      initialVoice: _initialVoiceModes[id] ?? false,
    );
  }

  @override
  Future<Conversation> create(
    DateTime acknowledgedAt, {
    bool voice = false,
  }) async {
    final personas = await _request(
      'GET',
      '/personas',
      query: {'limit': '100', 'offset': '0'},
    ) as Map<String, dynamic>;
    final active = (personas['items'] as List)
        .cast<Map<String, dynamic>>()
        .where((p) => p['status'] == 'active');
    if (active.isEmpty) {
      throw const BackendFailure(
        BackendFailureKind.generic,
        code: 'NO_PERSONA',
      );
    }
    final json = await _request(
      'POST',
      '/sessions',
      body: {
        'persona_id': active.first['id'],
        'session_id': newConversationId(),
        'ai_notice_ack_at': acknowledgedAt.toUtc().toIso8601String(),
      },
    ) as Map<String, dynamic>;
    final conversation = Conversation.fromJson(json, [], initialVoice: voice);
    _initialVoiceModes[conversation.id] = voice;
    return conversation;
  }

  @override
  Future<String> send(String id, String turnId, String message) async {
    return (await sendDetailed(id, turnId, message)).text;
  }

  @override
  Future<ChatReply> sendDetailed(
    String id,
    String turnId,
    String message,
  ) async {
    final json = await _request(
      'POST',
      '/chat/text',
      body: {'session_id': id, 'turn_id': turnId, 'message': message},
    ) as Map<String, dynamic>;
    final reply = ChatReply.fromJson(json, expectedTurnId: turnId);
    (_replyDetails[id] ??= {})[turnId] = reply;
    return reply;
  }

  @override
  Future<SpeechAudio> synthesize(ChatReply reply) async {
    if (reply.turnId == null) throw const FormatException('Missing turn id');
    final json = await _request(
      'POST',
      '/chat/turns/${Uri.encodeComponent(reply.turnId!)}/tts',
    ) as Map<String, dynamic>;
    return SpeechAudio.fromJson(json, reply.turnId!);
  }

  @override
  Future<VoiceReply> sendVoice(
    String id,
    String turnId,
    VoiceRecording recording,
  ) async {
    recording.validate();
    final request = http.MultipartRequest('POST', _uri('/chat/voice'))
      ..fields.addAll({'session_id': id, 'turn_id': turnId})
      ..files.add(
        http.MultipartFile.fromBytes(
          'file',
          recording.bytes,
          filename: recording.fileName,
          contentType: MediaType.parse(recording.mimeType),
        ),
      );
    final json = await _perform(request) as Map<String, dynamic>;
    final result = VoiceReply.fromJson(json, turnId);
    (_replyDetails[id] ??= {})[turnId] = result.reply;
    return result;
  }

  @override
  Future<void> delete(String id) async {
    await _request('DELETE', '/sessions/${Uri.encodeComponent(id)}');
    _initialVoiceModes.remove(id);
    _replyDetails.remove(id);
  }
}

/// Keeps the turn ID stable for retries of the same failed message.
class ConversationChatService implements VoiceChatService {
  ConversationChatService(this.service, this.id);
  final ConversationService service;
  final String id;
  String? _pendingId, _pendingMessage;
  VoiceRecording? _pendingRecording;
  String? _pendingVoiceId;
  @override
  bool get isDemo => service.isDemo;
  @override
  Future<String> reply(String message) async {
    return (await replyDetailed(message)).text;
  }

  @override
  Future<ChatReply> replyDetailed(String message) async {
    if (_pendingMessage != message) _pendingId = newConversationId();
    _pendingMessage = message;
    final answer = service is DetailedConversationService
        ? await (service as DetailedConversationService).sendDetailed(
            id,
            _pendingId!,
            message,
          )
        : ChatReply(
            text: await service.send(id, _pendingId!, message),
            turnId: _pendingId,
          );
    _pendingId = null;
    _pendingMessage = null;
    return answer;
  }

  @override
  Future<SpeechAudio> synthesize(ChatReply reply) async {
    if (service is DetailedConversationService) {
      return (service as DetailedConversationService).synthesize(reply);
    }
    throw UnsupportedError('No speech service');
  }

  @override
  Future<VoiceReply> replyVoice(VoiceRecording recording) async {
    if (service is! VoiceConversationService) {
      throw UnsupportedError('No voice service');
    }
    if (!identical(_pendingRecording, recording)) {
      _pendingVoiceId = newConversationId();
    }
    _pendingRecording = recording;
    final turnId = _pendingVoiceId!;
    final result = await (service as VoiceConversationService).sendVoice(
      id,
      turnId,
      recording,
    );
    if (_pendingVoiceId == turnId && identical(_pendingRecording, recording)) {
      _pendingVoiceId = null;
      _pendingRecording = null;
    }
    return result;
  }
}
