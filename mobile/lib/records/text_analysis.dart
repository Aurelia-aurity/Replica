import 'dart:async';
import 'dart:convert';

import 'record_models.dart';

class ChatSpeaker {
  const ChatSpeaker(this.name, this.messages);
  final String name;
  final int messages;
}

/// Result of inspecting a `.txt` file on the device (SC-04 items 2–4).
///
/// Only speaker names and message counts are kept, in memory, and nothing is
/// sent anywhere. Message bodies are never stored.
class TextAnalysis {
  const TextAnalysis({
    required this.kakao,
    this.speakers = const [],
    this.messages = 0,
  });
  static const plain = TextAnalysis(kakao: false);
  final bool kakao;
  final List<ChatSpeaker> speakers;
  final int messages;

  /// Picking whose speech to learn and who provided the file needs two people.
  bool get needsSpeakerSelection => kakao && speakers.length >= 2;
  String get label => kakao ? '카카오톡 대화로 인식됨' : '일반 텍스트';
}

// Android / PC export: `[이름] [오후 3:12] 내용`
final _bracket = RegExp(r'^\[([^\]]+)\] \[(?:오전|오후) \d{1,2}:\d{2}\] ');
// iOS / desktop export: `2025. 9. 20. 오후 3:12, 이름 : 내용` and variants.
final _dated = RegExp(
  r'^\d{4}(?:년 ?|[.\-/] ?)\d{1,2}(?:월 ?|[.\-/] ?)\d{1,2}(?:일|\.)?'
  r'(?: [가-힣]요일)? ?(?:(?:오전|오후) )?\d{1,2}:\d{2}(?::\d{2})?, (.+?) : ',
);

/// Reads the file as a stream; throws if the file cannot be read.
Future<TextAnalysis> analyzeTextRecord(RecordFile file) async {
  if (file.kind != RecordKind.text) return TextAnalysis.plain;
  final counts = <String, int>{};
  var total = 0;
  var first = true;
  final lines = const Utf8Decoder(allowMalformed: true)
      .bind(file.openRead())
      .transform(const LineSplitter());
  await for (var line in lines) {
    if (first) {
      first = false;
      if (line.startsWith('\uFEFF')) line = line.substring(1);
    }
    final match = _bracket.firstMatch(line) ?? _dated.firstMatch(line);
    final name = match?.group(1)?.trim();
    if (name == null || name.isEmpty) continue;
    counts.update(name, (n) => n + 1, ifAbsent: () => 1);
    total++;
  }
  // A single stray timestamp is not a chat export.
  if (total < 2) return TextAnalysis.plain;
  final speakers = [
    for (final entry in counts.entries) ChatSpeaker(entry.key, entry.value),
  ]..sort((a, b) => b.messages.compareTo(a.messages));
  return TextAnalysis(kakao: true, speakers: speakers, messages: total);
}

String formatCount(int value) {
  final digits = value.toString();
  final out = StringBuffer();
  for (var i = 0; i < digits.length; i++) {
    if (i > 0 && (digits.length - i) % 3 == 0) out.write(',');
    out.write(digits[i]);
  }
  return out.toString();
}
