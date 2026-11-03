import 'dart:convert';
import 'dart:typed_data';

/// Frontend view data. Evidence fields are not yet defined by the draft API.
enum EvidenceState { unknown, available, none }

class EvidenceRecord {
  const EvidenceRecord({
    required this.fileName,
    required this.date,
    required this.excerpt,
  });
  final String fileName, excerpt;
  final DateTime date;

  factory EvidenceRecord.fromJson(Map<String, dynamic> json) {
    final fileName = json['file_name'];
    final date = json['date'];
    final excerpt = json['excerpt'];
    final parsedDate = date is String ? DateTime.tryParse(date) : null;
    if (fileName is! String ||
        fileName.trim().isEmpty ||
        parsedDate == null ||
        excerpt is! String ||
        excerpt.trim().isEmpty) {
      throw const FormatException('Invalid evidence source');
    }
    return EvidenceRecord(
      fileName: fileName.trim(),
      date: parsedDate,
      excerpt: excerpt.trim(),
    );
  }
}

class ReplyEvidence {
  const ReplyEvidence(this.state, this.sources);
  final EvidenceState state;
  final List<EvidenceRecord> sources;

  /// Missing evidence is deliberately different from an explicit `none`.
  /// The backend must send `{status, items}` to make an evidence claim.
  factory ReplyEvidence.fromJson(Map<String, dynamic> json) {
    if (!json.containsKey('evidence') || json['evidence'] == null) {
      return const ReplyEvidence(EvidenceState.unknown, []);
    }
    final payload = json['evidence'];
    if (payload is! Map) throw const FormatException('Invalid evidence');
    final status = payload['status'];
    final rawItems = payload['items'];
    if (status == 'unavailable' || status == 'unknown') {
      return const ReplyEvidence(EvidenceState.unknown, []);
    }
    if (rawItems != null && rawItems is! List) {
      throw const FormatException('Invalid evidence items');
    }
    final items = <EvidenceRecord>[];
    for (final item in rawItems ?? const <Object>[]) {
      if (item is! Map) throw const FormatException('Invalid evidence item');
      items.add(EvidenceRecord.fromJson(item.cast<String, dynamic>()));
    }
    if (status == 'none') {
      if (items.isNotEmpty) {
        throw const FormatException('No-evidence response has sources');
      }
      return const ReplyEvidence(EvidenceState.none, []);
    }
    if (status == 'available' && items.isNotEmpty) {
      return ReplyEvidence(EvidenceState.available, List.unmodifiable(items));
    }
    throw const FormatException('Invalid evidence status');
  }
}

class ChatReply {
  const ChatReply({
    required this.text,
    this.turnId,
    this.modelName,
    this.finishReason,
    this.timings = const {},
    this.evidence = EvidenceState.unknown,
    this.sources = const [],
  });
  final String text;
  final String? turnId, modelName, finishReason;
  final Map<String, int?> timings;
  final EvidenceState evidence;
  final List<EvidenceRecord> sources;

  factory ChatReply.fromJson(
    Map<String, dynamic> json, {
    required String expectedTurnId,
  }) {
    if (json['turn_id'] != expectedTurnId ||
        json['is_ai_generated'] != true ||
        json['answer'] is! String ||
        (json['answer'] as String).trim().isEmpty ||
        (json['finish_reason'] != null &&
            !['stop', 'length'].contains(json['finish_reason']))) {
      throw const FormatException('Invalid AI response');
    }
    final timings = <String, int?>{};
    if (json['timings'] is Map) {
      for (final key in ['stt_ms', 'llm_ms', 'tts_ms', 'total_ms']) {
        final value = json['timings'][key];
        if (value != null && (value is! int || value < 0)) {
          throw const FormatException('Invalid timing');
        }
        timings[key] = value as int?;
      }
    }
    final evidence = ReplyEvidence.fromJson(json);
    return ChatReply(
      text: json['answer'] as String,
      turnId: expectedTurnId,
      modelName: json['model_name'] as String?,
      finishReason: json['finish_reason'] as String?,
      timings: Map.unmodifiable(timings),
      evidence: evidence.state,
      sources: evidence.sources,
    );
  }

  ChatReply withTurnId(String id) => ChatReply(
    text: text,
    turnId: id,
    modelName: modelName,
    finishReason: finishReason,
    timings: timings,
    evidence: evidence,
    sources: sources,
  );
}

class SpeechAudio {
  const SpeechAudio(this.bytes, this.mimeType);
  final Uint8List bytes;
  final String mimeType;

  factory SpeechAudio.fromJson(Map<String, dynamic> json, String turnId) {
    final encoded = json['audio_base64'];
    final mime = switch (json['audio_format']) {
      'wav' => 'audio/wav',
      'mp3' || 'mpeg' => 'audio/mpeg',
      _ => null,
    };
    if (json['turn_id'] != turnId ||
        json['tts_status'] != 'ready' ||
        mime == null ||
        encoded is! String ||
        encoded.isEmpty ||
        encoded.length > 35000000) {
      throw const FormatException('Speech unavailable');
    }
    final bytes = base64Decode(encoded);
    if (bytes.isEmpty) throw const FormatException('Empty speech');
    return SpeechAudio(bytes, mime);
  }
}
