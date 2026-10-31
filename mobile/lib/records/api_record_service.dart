import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../auth/auth_service.dart';
import '../services/backend_service.dart';
import 'record_models.dart';
import 'record_service.dart';
import 'record_review.dart';
import 'record_deletion.dart';

/// Supply only after agreeing the upload protocol. Must reconcile an ambiguous
/// previous transfer, preserve record ID and never overwrite an existing original.
abstract class RecordFileTransport {
  Future<void> ensureUploaded(RecordUpload upload);
}

/// Adapter for the supplied 2026-10-13 *draft*. Not enabled by main.dart.
/// No invented file endpoint, bucket path, persona ID or server size limit.
class ApiRecordService extends RecordService {
  ApiRecordService({
    required this.client,
    required this.baseUrl,
    required this.auth,
    required this.personaId,
    required this.policy,
    required this.fileTransport,
  }) : _backend = BackendService(auth);
  final http.Client client;
  final Uri baseUrl;
  final AuthService auth;
  final String personaId;
  @override
  final RecordPolicy policy;
  final RecordFileTransport fileTransport;
  final BackendService _backend;
  final _confirmedIds = <String>{};
  @override
  bool get isPreview => false;
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

  @override
  Future<RecordReview> review(String recordId) async {
    final record = await _find(recordId);
    if (record == null || record.status == RecordStatus.deleting) {
      throw StateError('Record missing');
    }
    if (record.status != RecordStatus.ready) {
      return RecordReview(record: record);
    }
    final response = await _request('GET', '/records/$recordId/text-versions');
    final versions =
        ((jsonDecode(response.body) as Map<String, dynamic>)['items'] as List)
            .map((e) => TextVersion.fromJson(e as Map<String, dynamic>))
            .where(
              (e) =>
                  e.recordId == recordId &&
                  (e.status == 'draft' || e.status == 'confirmed'),
            )
            .toList()
          ..sort((a, b) => b.revision.compareTo(a.revision));
    var profile = ReviewProfileState.unknown;
    if (versions.isNotEmpty && versions.first.status == 'draft') {
      _confirmedIds.remove(recordId);
    }
    if (versions.isNotEmpty && versions.first.status == 'confirmed') {
      _confirmedIds.add(recordId);
      // A ready persona profile does not prove this record has been indexed.
      try {
        final result = await _request(
          'GET',
          '/personas/$personaId/style-profile',
        );
        final body = jsonDecode(result.body);
        if (body is Map && body['status'] == 'ready') {
          profile = ReviewProfileState.available;
        }
      } on BackendFailure catch (failure) {
        if (failure.kind == BackendFailureKind.unauthenticated ||
            failure.kind == BackendFailureKind.staleSession) {
          rethrow;
        }
      } on FormatException {
        /* Keep confirmed text available if profile response is malformed. */
      }
    }
    return RecordReview(
      record: versions.isNotEmpty && versions.first.status == 'confirmed'
          ? record.withTextConfirmed()
          : record,
      version: versions.isEmpty ? null : versions.first,
      profile: profile,
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
    _confirmedIds.add(version.recordId);
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
        var record = RecordEntry.fromJson(item as Map<String, dynamic>);
        if (_confirmedIds.contains(record.id)) {
          record = record.withTextConfirmed();
        }
        if (record.status != RecordStatus.deleting) records[record.id] = record;
      }
      if (items.length < 100) return records.values.toList();
    }
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
  Future<void> upload(
    RecordUpload upload,
    void Function() onStageChanged,
  ) async {
    final validation = policy.validate(upload.file);
    if (validation != null) throw StateError(validation);
    if (upload.consentVersion != policy.consentVersion) {
      throw StateError('Consent version changed');
    }
    if (upload.stage == UploadStage.reserve) {
      RecordEntry? existing;
      if (upload.reservationRequested) existing = await _find(upload.id);
      if (existing == null) {
        upload.reservationRequested = true;
        final response = await _request(
          'POST',
          '/records',
          body: upload.reservationJson(personaId),
        );
        existing = RecordEntry.fromJson(
          jsonDecode(response.body) as Map<String, dynamic>,
        );
      }
      if (existing.id != upload.id) throw StateError('Record ID mismatch');
      if (existing.status == RecordStatus.deleting) {
        throw StateError('Record deleted');
      }
      upload.stage = existing.status == RecordStatus.uploading
          ? UploadStage.transfer
          : UploadStage.done;
      onStageChanged();
    }
    if (upload.stage == UploadStage.transfer) {
      await _backend.run(() => fileTransport.ensureUploaded(upload));
      upload.stage = UploadStage.complete;
      onStageChanged();
    }
    if (upload.stage == UploadStage.complete) {
      // A previous completion may have succeeded even if its response was lost.
      final existing = await _find(upload.id);
      if (existing == null) throw StateError('Record no longer exists');
      if (existing.status == RecordStatus.deleting) {
        throw StateError('Record deleted');
      }
      if (existing.status == RecordStatus.uploading) {
        await _request('POST', '/records/${upload.id}/uploaded');
      }
      upload.stage = UploadStage.done;
      onStageChanged();
    }
  }
}
