import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../auth/auth_service.dart';
import '../services/backend_service.dart';
import 'record_deletion.dart';
import 'record_models.dart';
import 'record_playback.dart';
import 'record_review.dart';
import 'record_service.dart';

/// Sends one original file, normally to the signed upload of the record's
/// latest reservation (`upload.uploadTarget`). Report progress with
/// `upload.reportProgress`, stop with `UploadCancelled` once
/// `upload.cancelToken` is cancelled, and raise [RecordStorageRejected] when
/// Storage refuses the request. See `HttpRecordFileTransport`.
abstract class RecordFileTransport {
  Future<void> ensureUploaded(RecordUpload upload);
}

/// Storage refused the transfer (for example an expired or rejected
/// signature). Storage is not the backend: this never ends the session, and
/// the record is reserved again for a new signature.
class RecordStorageRejected implements Exception {
  const RecordStorageRejected(this.statusCode);
  final int statusCode;
  @override
  String toString() => 'RecordStorageRejected($statusCode)';
}

/// Authenticated access to the backend for the optional hooks below.
abstract interface class RecordApiClient {
  Future<http.Response> send(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, Object>? body,
  });
}

/// Capabilities the 2026-10-13 draft does not define. Each stays disabled
/// (and its button hidden or inert) until the app and backend agree on the
/// request; the agreed request is then plugged in here, not guessed in the app.
class RecordApiExtensions {
  const RecordApiExtensions({
    this.retryProcessing,
    this.retryDeletion,
    this.originalAudio,
    this.deleteAccount,
  });

  /// Asks the server to run speech recognition again for a failed record.
  final Future<void> Function(RecordApiClient api, String recordId)?
  retryProcessing;

  /// Resumes a failed deletion worker for the same job.
  final Future<DeletionJob> Function(RecordApiClient api, DeletionJob job)?
  retryDeletion;

  /// Resolves where the original audio of a record can be streamed from.
  final Future<RecordAudioSource> Function(
    RecordApiClient api,
    String recordId,
  )?
  originalAudio;

  /// Starts deletion of the whole account and returns its job.
  final Future<DeletionJob> Function(RecordApiClient api)? deleteAccount;
}

/// Adapter for the supplied 2026-10-13 *draft*. Not enabled by main.dart.
/// No invented file endpoint, bucket path, persona ID or server size limit.
class ApiRecordService extends RecordService implements RecordApiClient {
  ApiRecordService({
    required this.client,
    required this.baseUrl,
    required this.auth,
    required this.personaId,
    required this.policy,
    required this.fileTransport,
    this.extensions = const RecordApiExtensions(),
    this.clock = DateTime.now,
  }) : _backend = BackendService(auth);

  /// Used to tell whether a signed upload URL has expired.
  final DateTime Function() clock;

  /// A signature this close to expiring is renewed before sending, so it does
  /// not run out between the check and the request.
  static const signatureMargin = Duration(seconds: 30);
  final http.Client client;
  final Uri baseUrl;
  final AuthService auth;
  final String personaId;
  final RecordApiExtensions extensions;
  @override
  final RecordPolicy policy;
  final RecordFileTransport fileTransport;
  final BackendService _backend;
  @override
  bool get isPreview => false;
  @override
  bool get canRetryProcessing => extensions.retryProcessing != null;
  @override
  bool get canRetryDeletion => extensions.retryDeletion != null;
  @override
  bool get canDeleteAccount => extensions.deleteAccount != null;
  @override
  bool get canPlayOriginal => extensions.originalAudio != null;

  @override
  Future<http.Response> send(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, Object>? body,
  }) => _request(method, path, query: query, body: body);

  @override
  Future<void> retryProcessing(String id) {
    final hook = extensions.retryProcessing;
    if (hook == null) throw UnsupportedError('Retry API not agreed');
    return hook(this, id);
  }

  @override
  Future<DeletionJob> retryDeletion(DeletionJob job) {
    final hook = extensions.retryDeletion;
    if (hook == null) throw UnsupportedError('Retry contract not agreed');
    return hook(this, job);
  }

  @override
  Future<DeletionJob> deleteAccount() {
    final hook = extensions.deleteAccount;
    if (hook == null) throw UnsupportedError('Account deletion not agreed');
    return hook(this);
  }

  @override
  Future<RecordAudioSource> originalAudio(String recordId) {
    final hook = extensions.originalAudio;
    if (hook == null) throw UnsupportedError('Original audio not agreed');
    return hook(this, recordId);
  }

  @override
  Future<DeletionJob> deleteRecord(String recordId) async =>
      DeletionJob.fromJson(
        jsonDecode((await _request('DELETE', '/records/$recordId')).body)
            as Map<String, dynamic>,
      );
  @override
  Future<DeletionJob> deletionJob(String jobId) async => DeletionJob.fromJson(
    jsonDecode((await _request('GET', '/deletion-jobs/$jobId')).body)
        as Map<String, dynamic>,
  );
  @override
  Future<List<DeletionJob>> deletionJobs() async {
    final jobs = <String, DeletionJob>{};
    for (var offset = 0; ; offset += 100) {
      final response = await _request(
        'GET',
        '/deletion-jobs',
        query: {'limit': '100', 'offset': '$offset'},
      );
      final items =
          (jsonDecode(response.body) as Map<String, dynamic>)['items'] as List;
      for (final item in items) {
        final job = DeletionJob.fromJson(item as Map<String, dynamic>);
        jobs[job.id] = job;
      }
      if (items.length < 100) return jobs.values.toList();
    }
  }

  bool _fatal(BackendFailure failure) =>
      failure.kind == BackendFailureKind.unauthenticated ||
      failure.kind == BackendFailureKind.staleSession;

  Future<List<TextVersion>> _versions(String recordId) async {
    final response = await _request('GET', '/records/$recordId/text-versions');
    return ((jsonDecode(response.body) as Map<String, dynamic>)['items']
            as List)
        .map((e) => TextVersion.fromJson(e as Map<String, dynamic>))
        .where(
          (e) =>
              e.recordId == recordId &&
              (e.status == 'draft' || e.status == 'confirmed'),
        )
        .toList()
      ..sort((a, b) => b.revision.compareTo(a.revision));
  }

  RecordEntry _confirmed(RecordEntry record, DateTime confirmedAt) =>
      record.copyWith(
        textConfirmed: true,
        confirmedAt: confirmedAt,
        personalization: ReviewProfileState.reflected,
      );

  @override
  Future<RecordReview> review(String recordId) async {
    final record = await _find(recordId);
    if (record == null || record.status == RecordStatus.deleting) {
      throw StateError('Record missing');
    }
    if (record.status != RecordStatus.ready) {
      return RecordReview(record: record, profile: record.personalization);
    }
    final versions = await _versions(recordId);
    final latest = versions.isEmpty ? null : versions.first;
    if (latest == null || latest.status != 'confirmed') {
      return RecordReview(
        record: record,
        version: latest,
        profile: record.personalization,
      );
    }
    final at = latest.confirmedAt ?? latest.updatedAt;
    final enriched = _confirmed(record, at);
    return RecordReview(
      record: enriched,
      version: latest,
      profile: enriched.personalization,
    );
  }

  @override
  Future<TextVersion> saveText(TextVersion version, String content) async {
    if (version.status != 'draft' || content.trim().isEmpty) {
      throw StateError('Invalid draft');
    }
    final response = await _request(
      'PATCH',
      '/text-versions/${version.id}',
      body: {
        'content': content,
        'expected_updated_at': version.updatedAt.toUtc().toIso8601String(),
      },
    );
    final saved = TextVersion.fromJson(
      jsonDecode(response.body) as Map<String, dynamic>,
    );
    if (saved.id != version.id ||
        saved.recordId != version.recordId ||
        saved.status != 'draft') {
      throw StateError('Invalid version');
    }
    return saved;
  }

  @override
  Future<TextVersion> confirmText(TextVersion version) async {
    // Reconcile a previous confirmation whose response was lost.
    final latest = await review(version.recordId);
    if (latest.version?.id != version.id) {
      throw const BackendFailure(BackendFailureKind.generic, code: 'CONFLICT');
    }
    if (latest.version!.status == 'confirmed') return latest.version!;
    if (latest.version!.updatedAt != version.updatedAt) {
      throw const BackendFailure(BackendFailureKind.generic, code: 'CONFLICT');
    }
    final response = await _request(
      'POST',
      '/text-versions/${version.id}/confirm',
    );
    final confirmed = TextVersion.fromJson(
      jsonDecode(response.body) as Map<String, dynamic>,
    );
    if (confirmed.id != version.id ||
        confirmed.recordId != version.recordId ||
        confirmed.status != 'confirmed' ||
        confirmed.confirmedAt == null) {
      throw StateError('Invalid confirmation');
    }
    return confirmed;
  }

  Future<http.Response> _request(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, Object>? body,
    bool allowNotFound = false,
  }) => _backend.run(() async {
    final headers = await auth.authorizationHeaders();
    final uri = baseUrl.replace(
      path: '${baseUrl.path.replaceFirst(RegExp(r'/$'), '')}$path',
      queryParameters: query,
    );
    final request = http.Request(method, uri)..headers.addAll(headers);
    if (body != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    try {
      final response = await (() async => http.Response.fromStream(
        await client.send(request),
      ))().timeout(const Duration(seconds: 30));
      if (response.statusCode >= 200 && response.statusCode < 300 ||
          allowNotFound && response.statusCode == 404) {
        return response;
      }
      Object? parsed;
      try {
        parsed = jsonDecode(response.body);
      } catch (_) {
        /* No raw body in UI. */
      }
      throw BackendFailure.fromResponse(
        statusCode: response.statusCode,
        body: parsed,
      );
    } on TimeoutException {
      throw const BackendFailure(BackendFailureKind.network, retryable: true);
    } on http.ClientException {
      throw const BackendFailure(BackendFailureKind.network, retryable: true);
    }
  });

  @override
  Future<List<RecordEntry>> list() async {
    final records = <String, RecordEntry>{};
    for (var offset = 0; ; offset += 100) {
      final response = await _request(
        'GET',
        '/records',
        query: {'persona_id': personaId, 'limit': '100', 'offset': '$offset'},
      );
      final page = jsonDecode(response.body) as Map<String, dynamic>;
      final items = page['items'] as List;
      for (final item in items) {
        final record = RecordEntry.fromJson(item as Map<String, dynamic>);
        if (record.status != RecordStatus.deleting) records[record.id] = record;
      }
      if (items.length < 100) break;
    }
    return _withConfirmation(records.values.toList());
  }

  /// Rechecks each ready record's latest transcript confirmation.
  /// Confirmation evidence belongs only to this list request, since another
  /// device can create or confirm a new revision between requests.
  /// Looks at most a few records up at once;
  /// a lookup that fails leaves that record as "확인 필요" instead of failing
  /// the whole list.
  Future<List<RecordEntry>> _withConfirmation(List<RecordEntry> records) async {
    final confirmedAt = <String, DateTime>{};
    final lookups = records
        .where((r) => r.status == RecordStatus.ready)
        .toList();
    for (var i = 0; i < lookups.length; i += 4) {
      await Future.wait([
        for (final record in lookups.skip(i).take(4))
          () async {
            try {
              final versions = await _versions(record.id);
              final latest = versions.isEmpty ? null : versions.first;
              if (latest != null && latest.status == 'confirmed') {
                confirmedAt[record.id] = latest.confirmedAt ?? latest.updatedAt;
              }
            } on BackendFailure catch (failure) {
              if (_fatal(failure)) rethrow;
            } on FormatException {
              /* Leave this record unchecked. */
            }
          }(),
      ]);
    }
    if (confirmedAt.isEmpty) return records;
    return [
      for (final record in records)
        if (record.status == RecordStatus.ready &&
            confirmedAt.containsKey(record.id))
          _confirmed(record, confirmedAt[record.id]!)
        else
          record,
    ];
  }

  Future<RecordEntry?> _find(String id) async {
    final response = await _request('GET', '/records/$id', allowNotFound: true);
    return response.statusCode == 404
        ? null
        : RecordEntry.fromJson(
            jsonDecode(response.body) as Map<String, dynamic>,
          );
  }

  @override
  Future<DeletionJob?> discardUpload(RecordUpload upload) async {
    if (!upload.reservationRequested || upload.stage == UploadStage.done) {
      return null;
    }
    final existing = await _find(upload.id);
    if (existing == null) return null;
    // An upload that already completed is a finished record: keep it.
    if (existing.status != RecordStatus.uploading &&
        existing.status != RecordStatus.deleting) {
      return null;
    }
    // Deleting the same record again returns the existing job by contract.
    return deleteRecord(upload.id);
  }

  @override
  Future<void> upload(
    RecordUpload upload,
    void Function() onStageChanged,
  ) async {
    final validation = policy.validate(upload.file);
    if (validation != null) throw StateError(validation);
    if (upload.consentVersion != policy.consentVersion) {
      throw StateError('Consent version changed');
    }
    upload.cancelToken.throwIfCancelled();
    // One automatic renewal per attempt for an expired or refused signature;
    // a second refusal is left to the user's retry.
    var renewed = false;
    // One automatic resend when `/uploaded` finds no original.
    var resent = false;
    while (upload.stage != UploadStage.done) {
      if (upload.stage == UploadStage.complete) {
        resent = await _complete(upload, resend: !resent) || resent;
        onStageChanged();
        continue;
      }
      if (upload.stage == UploadStage.reserve) {
        await _reserve(upload);
        onStageChanged();
        continue;
      }
      upload.cancelToken.throwIfCancelled();
      // An earlier transfer may have stored the file even though its answer
      // was lost: ask the server first (contract: '끊긴 전송·응답 유실').
      if (upload.transferStarted && await _completedAlready(upload)) {
        upload.stage = UploadStage.done;
        onStageChanged();
        return;
      }
      upload.cancelToken.throwIfCancelled();
      if (upload.originalStored) {
        upload.stage = UploadStage.complete;
        onStageChanged();
        continue;
      }
      final target = upload.uploadTarget;
      if (target != null &&
          target.expiredAt(clock(), margin: signatureMargin)) {
        // Reserve again with the same ID before sending.
        upload.stage = UploadStage.reserve;
        if (renewed) throw _storageFailure(null);
        renewed = true;
        continue;
      }
      upload.reportProgress(0);
      upload.transferStarted = true;
      try {
        await _backend.run(() => fileTransport.ensureUploaded(upload));
      } on RecordStorageRejected catch (rejection) {
        upload.cancelToken.throwIfCancelled();
        // Expired or refused signature: reserve again with the same ID. The
        // next transfer asks `/uploaded` first, in case the file is stored.
        upload.stage = UploadStage.reserve;
        if (renewed) throw _storageFailure(rejection.statusCode);
        renewed = true;
        continue;
      }
      // The file is stored but the user cancelled meanwhile: discard it.
      upload.cancelToken.throwIfCancelled();
      upload.stage = UploadStage.complete;
      onStageChanged();
    }
  }

  /// Tells the server the original is in place. Returns true when the server
  /// had no original (409 `UPLOAD_MISSING`) and [resend] allowed sending it
  /// again; otherwise that failure is rethrown with the upload set to send the
  /// file on the next attempt.
  ///
  /// 409 `UPLOAD_SIZE_MISMATCH` is rethrown as is: a stored original cannot be
  /// replaced, so the caller deletes the record and starts a new one.
  Future<bool> _complete(RecordUpload upload, {required bool resend}) async {
    upload.cancelToken.throwIfCancelled();
    // A previous completion may have succeeded even if its response was lost.
    final existing = await _find(upload.id);
    upload.cancelToken.throwIfCancelled();
    if (existing == null) throw StateError('Record no longer exists');
    if (existing.status == RecordStatus.deleting) {
      throw StateError('Record deleted');
    }
    if (existing.status == RecordStatus.uploading) {
      try {
        await _request('POST', '/records/${upload.id}/uploaded');
      } on BackendFailure catch (failure) {
        if (failure.code != 'UPLOAD_MISSING') rethrow;
        // Send it again: to the same signature, or to a new one when the
        // reservation gave none (it had said the original was stored).
        upload.stage = upload.uploadTarget == null
            ? UploadStage.reserve
            : UploadStage.transfer;
        upload.originalStored = false;
        upload.transferStarted = false;
        if (!resend) rethrow;
        return true;
      }
    }
    upload.cancelToken.throwIfCancelled();
    upload.stage = UploadStage.done;
    return false;
  }

  /// Reserves the record, or reserves it again with the same ID, which returns
  /// a fresh signed upload (or `upload: null` when the original is already
  /// stored). A record that has moved past `uploading` is finished.
  Future<void> _reserve(RecordUpload upload) async {
    RecordEntry? existing;
    if (upload.reservationRequested) existing = await _find(upload.id);
    upload.cancelToken.throwIfCancelled();
    if (existing != null && existing.id != upload.id) {
      throw StateError('Record ID mismatch');
    }
    if (existing?.status == RecordStatus.deleting) {
      throw StateError('Record deleted');
    }
    if (existing != null && existing.status != RecordStatus.uploading) {
      upload.stage = UploadStage.done;
      return;
    }
    upload.reservationRequested = true;
    final response = await _request(
      'POST',
      '/records',
      body: upload.reservationJson(
        personaId,
        includeSpeakers: policy.sendSpeakerSelection,
      ),
    );
    upload.cancelToken.throwIfCancelled();
    final json = jsonDecode(response.body) as Map<String, dynamic>;
    final reserved = RecordEntry.fromJson(json);
    if (reserved.id != upload.id) throw StateError('Record ID mismatch');
    if (reserved.status == RecordStatus.deleting) {
      throw StateError('Record deleted');
    }
    // Every reservation answer replaces the previous signature.
    final info = json['upload'];
    upload.originalStored = json.containsKey('upload') && info == null;
    upload.uploadTarget = info == null
        ? null
        : RecordUploadTarget.fromJson(info);
    upload.stage = reserved.status == RecordStatus.uploading
        ? UploadStage.transfer
        : UploadStage.done;
  }

  /// Asks `/uploaded` whether an earlier transfer already stored the file.
  /// `UPLOAD_MISSING` means it did not, and the file is sent.
  Future<bool> _completedAlready(RecordUpload upload) async {
    try {
      await _request('POST', '/records/${upload.id}/uploaded');
      return true;
    } on BackendFailure catch (failure) {
      if (failure.code != 'UPLOAD_MISSING') rethrow;
      upload.transferStarted = false;
      return false;
    }
  }

  /// Storage still refuses after a renewed signature. Retrying the same
  /// record reserves it again, and the session stays signed in.
  BackendFailure _storageFailure(int? statusCode) => BackendFailure(
    BackendFailureKind.generic,
    code: 'STORAGE_REJECTED',
    retryable: true,
    statusCode: statusCode,
  );
}
