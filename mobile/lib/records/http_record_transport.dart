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
    // Each transfer has its own abort signal. A timeout must not cancel the
    // upload's token, since the same upload can be retried.
    final abort = Completer<void>();
    final interrupted = Completer<void>();
    final cancellation = token.whenCancelled.asStream().listen((_) {
      if (!interrupted.isCompleted) {
        interrupted.completeError(const UploadCancelled());
      }
      if (!abort.isCompleted) abort.complete();
    });
    final request = http.AbortableStreamedRequest(
      target.method,
      target.url,
      abortTrigger: abort.future,
    )..headers.addAll(target.headers);
    request.headers.putIfAbsent('content-type', () => upload.file.mimeType);
    request.contentLength = upload.file.sizeBytes;
    StreamSubscription<List<int>>? file;
    var sent = 0;
    void fail(Object error, StackTrace stack) {
      if (!interrupted.isCompleted) interrupted.completeError(error, stack);
    }

    late final StreamController<List<int>> body;
    body = StreamController<List<int>>(
      onListen: () {
        if (abort.isCompleted) {
          unawaited(body.close());
          return;
        }
        try {
          file = upload.file.openRead().listen(
            (chunk) {
              try {
                if (abort.isCompleted || interrupted.isCompleted) return;
                token.throwIfCancelled();
                sent += chunk.length;
                if (sent > upload.file.sizeBytes) {
                  throw const BackendFailure(
                    BackendFailureKind.generic,
                    code: 'FILE_CHANGED',
                  );
                }
                upload.reportProgress(sent);
                token.throwIfCancelled();
                body.add(chunk);
              } catch (error, stack) {
                fail(error, stack);
              }
            },
            onError: fail,
            onDone: () {
              if (!abort.isCompleted && sent != upload.file.sizeBytes) {
                fail(
                  const BackendFailure(
                    BackendFailureKind.generic,
                    code: 'FILE_CHANGED',
                  ),
                  StackTrace.current,
                );
              }
              unawaited(body.close());
            },
            cancelOnError: true,
          );
        } catch (error, stack) {
          fail(error, stack);
          unawaited(body.close());
        }
      },
      onPause: () => file?.pause(),
      onResume: () => file?.resume(),
      onCancel: () => file?.cancel(),
    );

    Future<void> receive() async {
      final response = await client.send(request);
      final result = await http.Response.fromStream(response);
      if (result.statusCode < 200 || result.statusCode >= 300) {
        throw BackendFailure.fromResponse(statusCode: result.statusCode);
      }
    }

    try {
      upload.reportProgress(0);
      final response = receive();
      final writing = request.sink.addStream(body.stream).whenComplete(() {
        // Closing a StreamedRequest can wait for the HTTP consumer. Abort and
        // file errors must still reach the caller if it stops consuming.
        unawaited(request.sink.close());
      });
      await Future.any<void>([
        Future.wait<void>([writing, response], eagerError: true).then((_) {}),
        interrupted.future,
      ]).timeout(timeout);
    } on UploadCancelled {
      rethrow;
    } on http.RequestAbortedException {
      throw const UploadCancelled();
    } on TimeoutException {
      throw const BackendFailure(BackendFailureKind.network, retryable: true);
    } on http.ClientException {
      if (token.isCancelled) throw const UploadCancelled();
      throw const BackendFailure(BackendFailureKind.network, retryable: true);
    } finally {
      // Stop both the network request and a stalled file read before allowing
      // another attempt to start. The shared HTTP client stays reusable.
      if (!abort.isCompleted) abort.complete();
      await cancellation.cancel();
      await file?.cancel();
      unawaited(body.close());
    }
  }
}
