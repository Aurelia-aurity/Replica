/// One timestamped piece of a transcript (SC-05 item 3).
///
/// The draft API stores a transcript as a single `content` string. Timing,
/// speaker and recognition-confidence fields are optional extensions: the app
/// shows them when the server provides them and never fabricates values.
class TranscriptSegment {
  const TranscriptSegment({
    required this.text,
    this.startMs,
    this.endMs,
    this.speaker,
    this.uncertain = const [],
  });
  final String text;
  final int? startMs;
  final int? endMs;
  final String? speaker;

  /// Words the recognizer was unsure about (dialect, forms of address).
  final List<String> uncertain;
  bool get timed => startMs != null;

  TranscriptSegment copyWith({String? text}) => TranscriptSegment(
    text: text ?? this.text,
    startMs: startMs,
    endMs: endMs,
    speaker: speaker,
    uncertain: uncertain,
  );

  /// Lenient: a malformed item is dropped rather than failing the whole record.
  static TranscriptSegment? tryParse(Object? json) {
    if (json is! Map) return null;
    final text = json['text'];
    if (text is! String) return null;
    int? ms(Object? value) => value is int && value >= 0 ? value : null;
    final start = ms(json['start_ms']);
    var end = ms(json['end_ms']);
    if (start != null && end != null && end < start) end = null;
    final speaker = json['speaker'];
    final words = json['uncertain_words'];
    return TranscriptSegment(
      text: text,
      startMs: start,
      endMs: end,
      speaker: speaker is String && speaker.trim().isNotEmpty ? speaker : null,
      uncertain: words is List ? words.whereType<String>().toList() : const [],
    );
  }
}

class TranscriptDocument {
  const TranscriptDocument(this.segments);
  final List<TranscriptSegment> segments;

  bool get hasTimestamps => segments.any((s) => s.timed);
  bool get hasSpeakers => segments.any((s) => s.speaker != null);
  String get content => segments.map((s) => s.text).join('\n');

  /// Server segments are usable only when they reproduce `content` exactly, so
  /// editing a segment can never silently diverge from the stored text.
  static List<TranscriptSegment>? validated(
    List<TranscriptSegment>? segments,
    String content,
  ) {
    if (segments == null || segments.isEmpty) return null;
    final joined = segments.map((s) => s.text).join('\n');
    return joined == content ? segments : null;
  }

  /// Segments for the text being edited. Timing and speaker metadata follow
  /// the base segments only while the segment count is unchanged.
  factory TranscriptDocument.fromContent(
    String content, {
    List<TranscriptSegment>? base,
  }) {
    final lines = content.split('\n');
    if (base != null && base.length == lines.length) {
      return TranscriptDocument([
        for (var i = 0; i < lines.length; i++) base[i].copyWith(text: lines[i]),
      ]);
    }
    return TranscriptDocument([
      for (final line in lines) TranscriptSegment(text: line),
    ]);
  }

  /// Index of the segment playing at [positionMs], or null before the first.
  int? activeIndexAt(int positionMs) {
    int? found;
    for (var i = 0; i < segments.length; i++) {
      final start = segments[i].startMs;
      if (start == null) continue;
      if (start <= positionMs) {
        found = i;
      } else {
        break;
      }
    }
    return found;
  }
}

/// `mm:ss`, or `h:mm:ss` from one hour.
String formatClock(Duration value) {
  final total = value.inSeconds < 0 ? 0 : value.inSeconds;
  final h = total ~/ 3600;
  final m = (total % 3600) ~/ 60;
  final s = total % 60;
  String two(int v) => v.toString().padLeft(2, '0');
  return h > 0 ? '$h:${two(m)}:${two(s)}' : '${two(m)}:${two(s)}';
}

String formatClockMs(int ms) => formatClock(Duration(milliseconds: ms));
