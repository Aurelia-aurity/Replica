import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

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

/// Owned by one login generation, never shared between accounts or persisted.
class MemoryConversationService implements ConversationService {
  MemoryConversationService({required this.backend, required this.chat});
  final BackendService backend;
  final ChatService chat;
  final _items = <String, Conversation>{};
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
  Future<String> send(String id, String turnId, String message) =>
      backend.run(() async {
        final before = _items[id]!;
        for (final turn in before.turns) {
          if (turn.id == turnId) return turn.answer!;
        }
        final answer = await backend.run(() => chat.reply(message));
        if (answer.trim().isEmpty) throw const FormatException('Empty answer');
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
              answer: answer,
            ),
          ],
        );
        return answer;
      });
  @override
  Future<void> delete(String id) => backend.run(() async {
    _items.remove(id);
  });
}

/// Adapter for the supplied draft API. No preview credentials are sent here.
class HttpConversationService implements ConversationService {
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
  @override
  bool get isDemo => false;

  Future<Object?> _request(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, Object?>? body,
  }) => backend.run(() async {
    final headers = await backend.auth.authorizationHeaders();
    // Check generation again after asynchronous token acquisition.
    return backend.run(() async {
      final uri = baseUrl.replace(
        path: '${baseUrl.path.replaceFirst(RegExp(r'/$'), '')}$path',
        queryParameters: query,
      );
      final request = http.Request(method, uri)
        ..headers.addAll({...headers, 'Content-Type': 'application/json'});
      if (body != null) request.body = jsonEncode(body);
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
      if (method == 'DELETE' && response.statusCode != 204) {
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
      final page = items.map(ConversationTurn.fromJson).toList();
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
    final json = await _request(
      'POST',
      '/chat/text',
      body: {'session_id': id, 'turn_id': turnId, 'message': message},
    ) as Map<String, dynamic>;
    if (json['turn_id'] != turnId ||
        json['is_ai_generated'] != true ||
        json['answer'] is! String ||
        (json['answer'] as String).trim().isEmpty) {
      throw const FormatException('Invalid AI response');
    }
    return json['answer'] as String;
  }

  @override
  Future<void> delete(String id) async {
    await _request('DELETE', '/sessions/${Uri.encodeComponent(id)}');
    _initialVoiceModes.remove(id);
  }
}

/// Keeps the turn ID stable for retries of the same failed message.
class ConversationChatService implements ChatService {
  ConversationChatService(this.service, this.id);
  final ConversationService service;
  final String id;
  String? _pendingId, _pendingMessage;
  @override
  bool get isDemo => service.isDemo;
  @override
  Future<String> reply(String message) async {
    if (_pendingMessage != message) _pendingId = newConversationId();
    _pendingMessage = message;
    final answer = await service.send(id, _pendingId!, message);
    _pendingId = null;
    _pendingMessage = null;
    return answer;
  }
}
