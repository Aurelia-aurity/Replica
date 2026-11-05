import 'dart:async';

import 'package:http/http.dart' as http;

import '../services/backend_service.dart';
import 'api_record_service.dart';
import 'record_models.dart';

/// Where and how one original file is sent. Supplied by the app once the
/// upload protocol is agreed (direct Storage upload or through the backend);
/// nothing here guesses a URL, bucket path or header.
class RecordUploadTarget {
  const RecordUploadTarget({
    required this.url,
    this.method = 'PUT',
    this.headers = const {},
  });
  final Uri url;
  final String method;
  final Map<String, String> headers;
}

/// Streams the picked file to an agreed [RecordUploadTarget] with real
/// progress and cancellation.
///
/// Reconciling an ambiguous earlier attempt and refusing to overwrite an
/// existing original stay the responsibility of the agreed protocol:
/// [alreadyStored] lets the app confirm a previous transfer, and the target
/// headers must make the server reject an overwrite.
class HttpRecordFileTransport extends RecordFileTransport {
  HttpRecordFileTransport({
    required this.client,
    required this.resolveTarget,
    this.alreadyStored,
    this.timeout = const Duration(minutes: 10),
  });
  final http.Client client;
  final Future<RecordUploadTarget> Function(RecordUpload upload) resolveTarget;

  /// Returns true when the original of [upload] is already stored completely.
  final Future<bool> Function(RecordUpload upload)? alreadyStored;
  final Duration timeout;

  @override
  Future<void> ensureUploaded(RecordUpload upload) async {
    final token = upload.cancelToken;
    token.throwIfCancelled();
    if (alreadyStored != null && await alreadyStored!(upload)) {
      upload.reportProgress(upload.file.sizeBytes);
      return;
    }
    final target = await resolveTarget(upload);
    token.throwIfCancelled();
    final request = http.AbortableStreamedRequest(
      target.method,
      target.url,
      abortTrigger: token.whenCancelled,
    )..headers.addAll(target.headers);
    request.headers.putIfAbsent('content-type', () => upload.file.mimeType);
    request.contentLength = upload.file.sizeBytes;
    var sent = 0;
    upload.reportProgress(0);
    final body = upload.file.openRead().map((chunk) {
      token.throwIfCancelled();
      sent += chunk.length;
      if (sent > upload.file.sizeBytes) {
        // The file grew after it was picked; never send more than reserved.
        throw const BackendFailure(
          BackendFailureKind.generic,
          code: 'FILE_CHANGED',
        );
      }
      upload.reportProgress(sent);
      return chunk;
    });
    try {
      final response = client.send(request);
      final writing = request.sink
          .addStream(body)
          .whenComplete(request.sink.close);
      // A failed stream (cancel, changed file) must surface even though the
      // client is still waiting for the request body.
      await Future.wait<Object?>([writing, response]).timeout(timeout);
      final done = await response;
      final result = await http.Response.fromStream(done).timeout(timeout);
      if (result.statusCode < 200 || result.statusCode >= 300) {
        throw BackendFailure.fromResponse(statusCode: result.statusCode);
      }
      if (sent != upload.file.sizeBytes) {
        throw const BackendFailure(
          BackendFailureKind.generic,
          code: 'FILE_CHANGED',
        );
      }
    } on UploadCancelled {
      rethrow;
    } on http.RequestAbortedException {
      throw const UploadCancelled();
    } on TimeoutException {
      throw const BackendFailure(BackendFailureKind.network, retryable: true);
    } on http.ClientException {
      if (token.isCancelled) throw const UploadCancelled();
      throw const BackendFailure(BackendFailureKind.network, retryable: true);
    }
  }
}
