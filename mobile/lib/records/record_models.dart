import 'dart:async';
import 'dart:math';

enum RecordKind { text, audio }

enum RecordStatus { uploading, uploaded, processing, ready, failed, deleting }

/// How far a confirmed transcript has reached the personalization pipeline.
///
/// For the current UI, reflected means text upload or audio transcript
/// confirmation was saved successfully. Profile/index completion is separate.
enum ReviewProfileState {
  unknown,
  available,
  processing,
  reflected,
  delayed,
  failed,
}

class RecordEntry {
  const RecordEntry({
    required this.id,
    required this.name,
    required this.kind,
    required this.status,
    required this.createdAt,
    this.sizeBytes,
    this.durationSeconds,
    bool confirmed = false,
    this.textConfirmed = false,
    ReviewProfileState? personalization,
    this.confirmedAt,
    this.errorCode,
  }) : personalization =
           personalization ??
           (confirmed ||
                   (kind == RecordKind.text &&
                       (status == RecordStatus.uploaded ||
                           status == RecordStatus.processing ||
                           status == RecordStatus.ready))
               ? ReviewProfileState.reflected
               : ReviewProfileState.unknown);
  final String id;
  final String name;
  final RecordKind kind;
  final RecordStatus status;
  final DateTime createdAt;
  final int? sizeBytes;
  final int? durationSeconds;

  /// A server-confirmed transcript, separate from the stored-text UI badge.
  final bool textConfirmed;
  final DateTime? confirmedAt;
  final ReviewProfileState personalization;

  /// Safe failure code from the server record (never a raw message).
  final String? errorCode;

  /// Saved successfully according to the current UI completion rule.
  bool get confirmed => personalization == ReviewProfileState.reflected;

  /// `/uploaded` refused the speakers (`error_code: speaker_*`) and the record
  /// waits in `uploading` for the user to choose them. Not in progress: it
  /// does not move until the user opens it.
  bool get needsSpeakerCheck =>
      status == RecordStatus.uploading &&
      (errorCode?.startsWith('speaker_') ?? false);

  bool get inProgress =>
      !confirmed &&
      !needsSpeakerCheck &&
      (status == RecordStatus.uploading ||
          status == RecordStatus.uploaded ||
          status == RecordStatus.processing ||
          (status == RecordStatus.ready &&
              personalization == ReviewProfileState.processing));

  RecordEntry copyWith({
    RecordStatus? status,
    bool? textConfirmed,
    ReviewProfileState? personalization,
    DateTime? confirmedAt,
    String? errorCode,
  }) => RecordEntry(
    id: id,
    name: name,
    kind: kind,
    status: status ?? this.status,
    createdAt: createdAt,
    sizeBytes: sizeBytes,
    durationSeconds: durationSeconds,
    textConfirmed: textConfirmed ?? this.textConfirmed,
    personalization: personalization ?? this.personalization,
    confirmedAt: confirmedAt ?? this.confirmedAt,
    errorCode: errorCode ?? this.errorCode,
  );

  RecordEntry withTextConfirmed() => copyWith(textConfirmed: true);

  factory RecordEntry.fromJson(Map<String, dynamic> json) => RecordEntry(
    id: json['id'] as String,
    name: json['original_name'] as String,
    kind: RecordKind.values.byName(json['kind'] as String),
    status: RecordStatus.values.byName(json['status'] as String),
    createdAt: DateTime.parse(json['created_at'] as String),
    sizeBytes: json['size_bytes'] as int?,
    errorCode: json['error_code'] as String?,
  );

  String get statusLabel => switch (status) {
    RecordStatus.uploading =>
      needsSpeakerCheck
          ? '확인 필요'
          : kind == RecordKind.audio
          ? '전사 중'
          : '반영 중',
    RecordStatus.uploaded || RecordStatus.processing =>
      confirmed
          ? '반영됨'
          : kind == RecordKind.audio
          ? '전사 중'
          : '반영 중',
    RecordStatus.ready => switch (personalization) {
      ReviewProfileState.reflected => '반영됨',
      ReviewProfileState.processing => '반영 중',
      ReviewProfileState.delayed => '반영 지연',
      ReviewProfileState.failed => '반영 실패',
      _ => textConfirmed ? '반영됨' : '확인 필요',
    },
    RecordStatus.failed => '실패',
    RecordStatus.deleting => '삭제 중',
  };

  String get failureLabel =>
      kind == RecordKind.audio ? '전사에 실패했어요' : '기록 처리에 실패했어요';
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
  const RecordPolicy({
    required this.maxBytes,
    required this.consentVersion,
    this.sendSpeakerSelection = true,
  });
  final int maxBytes;
  final String consentVersion;

  /// Sends `target_speaker` (and `provider_speaker` when the chat has two or
  /// more speakers) with the reservation, as the app-backend contract
  /// specifies. Turn it off to keep the selection on the device only.
  final bool sendSpeakerSelection;
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

/// Where and how one original file is sent: the signed upload information
/// `POST /records` returns as `upload`. The app adds no URL, bucket path or
/// authorization header of its own.
class RecordUploadTarget {
  const RecordUploadTarget({
    required this.url,
    this.method = 'PUT',
    this.headers = const {},
    this.expiresAt,
  });

  /// Reads the reservation's `upload` object; throws [FormatException] when a
  /// field is missing or has the wrong type.
  factory RecordUploadTarget.fromJson(Object? json) {
    if (json is! Map) throw const FormatException('upload is not an object');
    final url = json['url'];
    final method = json['method'];
    final headers = json['headers'] ?? const <String, Object?>{};
    final expiresAt = json['expires_at'];
    final parsed = url is String ? Uri.tryParse(url) : null;
    if (parsed == null || !parsed.hasScheme || parsed.host.isEmpty) {
      throw const FormatException('upload.url is not an absolute URL');
    }
    if (method is! String || method.isEmpty) {
      throw const FormatException('upload.method is missing');
    }
    if (headers is! Map ||
        headers.entries.any((e) => e.key is! String || e.value is! String)) {
      throw const FormatException('upload.headers is not a string map');
    }
    if (expiresAt is! String) {
      throw const FormatException('upload.expires_at is missing');
    }
    return RecordUploadTarget(
      url: parsed,
      method: method.toUpperCase(),
      headers: Map.unmodifiable(headers.cast<String, String>()),
      expiresAt: DateTime.parse(expiresAt),
    );
  }

  final Uri url;
  final String method;
  final Map<String, String> headers;

  /// After this the signature no longer works and the record is reserved
  /// again with the same ID for a new one.
  final DateTime? expiresAt;

  bool expiredAt(DateTime now, {Duration margin = Duration.zero}) {
    final end = expiresAt;
    return end != null && !now.add(margin).isBefore(end);
  }
}

/// Raised inside an upload when the user cancelled it.
class UploadCancelled implements Exception {
  const UploadCancelled();
}

class UploadCancelToken {
  final _completer = Completer<void>();
  bool get isCancelled => _completer.isCompleted;
  Future<void> get whenCancelled => _completer.future;
  void cancel() {
    if (!_completer.isCompleted) _completer.complete();
  }

  void throwIfCancelled() {
    if (isCancelled) throw const UploadCancelled();
  }
}

class RecordUpload {
  RecordUpload({
    required this.file,
    required this.consentAt,
    required this.consentVersion,
    this.targetSpeaker,
    this.providerSpeaker,
  }) : id = _uuid();
  final String id;
  final RecordFile file;
  final DateTime consentAt;
  final String consentVersion;

  /// A new record (new ID) for the same file, consent and speaker choice,
  /// for when the stored original cannot be fixed under this record.
  RecordUpload restart() => RecordUpload(
    file: file,
    consentAt: consentAt,
    consentVersion: consentVersion,
    targetSpeaker: targetSpeaker,
    providerSpeaker: providerSpeaker,
  );

  /// Chosen on SC-04 for KakaoTalk exports. Names stay in memory only. They
  /// change when the server asks to choose again after the upload.
  String? targetSpeaker;
  String? providerSpeaker;

  /// The speakers were chosen again after the server refused them, so the
  /// next `/uploaded` carries them (an explicit null clears a value).
  bool speakersChanged = false;

  /// Body of `POST /records/{id}/uploaded`. Leaving the keys out keeps the
  /// reserved values.
  Map<String, Object?> uploadedJson({bool includeSpeakers = false}) =>
      includeSpeakers && speakersChanged
      ? {'target_speaker': targetSpeaker, 'provider_speaker': providerSpeaker}
      : const {};
  UploadStage stage = UploadStage.reserve;
  bool reservationRequested = false;

  /// Signed upload information from the latest reservation, if it gave one.
  RecordUploadTarget? uploadTarget;

  /// The latest reservation answered `upload: null`: the original is already
  /// in Storage, so it is not sent again.
  bool originalStored = false;

  /// A transfer was started, so the original may already be stored. The next
  /// attempt asks `/uploaded` first instead of sending the file again.
  bool transferStarted = false;
  final cancelToken = UploadCancelToken();

  /// Bytes handed to the transport for the current transfer attempt.
  int bytesSent = 0;
  void Function()? onProgress;
  double? get progress => file.sizeBytes <= 0
      ? null
      : (bytesSent / file.sizeBytes).clamp(0.0, 1.0).toDouble();
  void reportProgress(int sent) {
    bytesSent = sent.clamp(0, file.sizeBytes);
    onProgress?.call();
  }

  Map<String, Object> reservationJson(
    String personaId, {
    bool includeSpeakers = false,
  }) => {
    'record_id': id,
    'persona_id': personaId,
    'kind': file.kind.name,
    'original_name': file.name,
    'mime_type': file.mimeType,
    'size_bytes': file.sizeBytes,
    'consent_at': consentAt.toUtc().toIso8601String(),
    'consent_version': consentVersion,
    if (includeSpeakers && targetSpeaker != null)
      'target_speaker': targetSpeaker!,
    if (includeSpeakers && providerSpeaker != null)
      'provider_speaker': providerSpeaker!,
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
