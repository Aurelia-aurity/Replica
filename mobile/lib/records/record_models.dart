import 'dart:math';

enum RecordKind { text, audio }

enum RecordStatus { uploading, uploaded, processing, ready, failed, deleting }

class RecordEntry {
  const RecordEntry({
    required this.id,
    required this.name,
    required this.kind,
    required this.status,
    required this.createdAt,
    this.sizeBytes,
    this.durationSeconds,
    this.confirmed = false,
    this.textConfirmed = false,
  });
  final String id;
  final String name;
  final RecordKind kind;
  final RecordStatus status;
  final DateTime createdAt;
  final int? sizeBytes;
  final int? durationSeconds;
  // The draft record object has no confirmation/profile-index evidence.
  // `ready` alone must never be interpreted as reflected personalization.
  final bool confirmed;
  final bool textConfirmed;
  RecordEntry withTextConfirmed() => RecordEntry(
    id: id,
    name: name,
    kind: kind,
    status: status,
    createdAt: createdAt,
    sizeBytes: sizeBytes,
    durationSeconds: durationSeconds,
    confirmed: confirmed,
    textConfirmed: true,
  );

  factory RecordEntry.fromJson(Map<String, dynamic> json) => RecordEntry(
    id: json['id'] as String,
    name: json['original_name'] as String,
    kind: RecordKind.values.byName(json['kind'] as String),
    status: RecordStatus.values.byName(json['status'] as String),
    createdAt: DateTime.parse(json['created_at'] as String),
    sizeBytes: json['size_bytes'] as int?,
  );

  String get statusLabel => switch (status) {
    RecordStatus.uploading => '업로드 중',
    RecordStatus.uploaded => '처리 대기',
    RecordStatus.processing => kind == RecordKind.audio ? '전사 중' : '처리 중',
    RecordStatus.ready =>
      confirmed
          ? '반영됨'
          : textConfirmed
          ? '확정됨'
          : '확인 필요',
    RecordStatus.failed => '실패',
    RecordStatus.deleting => '삭제 중',
  };
}

class RecordFile {
  const RecordFile({
    required this.name,
    required this.sizeBytes,
    required this.openRead,
  });
  final String name;
  final int sizeBytes;
  final Stream<List<int>> Function() openRead;
  String get extension =>
      name.contains('.') ? name.split('.').last.toLowerCase() : '';
  RecordKind get kind =>
      extension == 'txt' ? RecordKind.text : RecordKind.audio;
  String get mimeType => switch (extension) {
    'txt' => 'text/plain',
    'wav' => 'audio/wav',
    'mp3' => 'audio/mpeg',
    'm4a' => 'audio/mp4',
    _ => 'application/octet-stream',
  };
}

class RecordPolicy {
  const RecordPolicy({required this.maxBytes, required this.consentVersion});
  final int maxBytes;
  final String consentVersion;
  String? validate(RecordFile file) {
    if (!const ['txt', 'wav', 'mp3', 'm4a'].contains(file.extension)) {
      return '지원하지 않는 형식이에요. .txt, .wav, .mp3, .m4a 파일을 선택해 주세요.';
    }
    if (file.name.trim().isEmpty || file.name.runes.length > 255) {
      return '파일 이름은 1~255자로 지정해 주세요.';
    }
    if (file.sizeBytes <= 0) return '빈 파일은 올릴 수 없어요.';
    if (file.sizeBytes > maxBytes) {
      return '파일은 ${formatBytes(maxBytes)} 이하로 선택해 주세요.';
    }
    return null;
  }
}

enum UploadStage { reserve, transfer, complete, done }

class RecordUpload {
  RecordUpload({
    required this.file,
    required this.consentAt,
    required this.consentVersion,
  }) : id = _uuid();
  final String id;
  final RecordFile file;
  final DateTime consentAt;
  final String consentVersion;
  UploadStage stage = UploadStage.reserve;
  bool reservationRequested = false;
  Map<String, Object> reservationJson(String personaId) => {
    'record_id': id,
    'persona_id': personaId,
    'kind': file.kind.name,
    'original_name': file.name,
    'mime_type': file.mimeType,
    'size_bytes': file.sizeBytes,
    'consent_at': consentAt.toUtc().toIso8601String(),
    'consent_version': consentVersion,
  };
}

String _uuid() {
  final random = Random.secure();
  final bytes = List.generate(16, (_) => random.nextInt(256));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}

String formatBytes(int bytes) => bytes < 1024
    ? '$bytes B'
    : bytes < 1024 * 1024
    ? '${(bytes / 1024).toStringAsFixed(1)} KB'
    : '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';
