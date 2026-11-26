import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/records/api_record_service.dart';
import 'package:mobile/records/http_record_transport.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/services/backend_service.dart';

import 'records_test.dart' show ApiTestAuth, file, recordJson;

/// What the synthetic Storage does with one `PUT`.
enum _Put {
  /// Stores the file and answers 200.
  ok,

  /// Refuses the signature without storing anything.
  unauthorized,
  forbidden,

  /// Stores the file, then the connection drops before the answer.
  lostAfterStore,

  /// Answers 200 but nothing ends up stored.
  okButMissing,
}

final _now = DateTime.utc(2026, 11, 27, 9);

Map<String, Object?> _signed(
  int n, {
  Duration valid = const Duration(hours: 1),
}) => {
  'url': 'https://storage.invalid/signed/$n',
  'method': 'PUT',
  'headers': {'Content-Type': 'text/plain', 'x-upsert': 'false'},
  'expires_at': _now.add(valid).toIso8601String(),
};

/// A synthetic backend and Storage following the app-backend contract.
class _Server {
  final calls = <String>[];
  final reservationIds = <String>[];
  final storageRequests = <http.BaseRequest>[];
  final puts = <_Put>[];

  /// The `upload` value of each reservation in turn; a fresh signature when
  /// the list runs out. Use [_absent] to leave the key out.
  final uploads = <Object?>[];
  var stored = false;
  var status = 'uploading';
  var reservations = 0;

  late final client = MockClient.streaming((request, body) async {
    final bytes = await body.toBytes();
    if (request.url.host == 'storage.invalid') {
      calls.add('PUT ${request.url.path}');
      storageRequests.add(request);
      switch (puts.isEmpty ? _Put.ok : puts.removeAt(0)) {
        case _Put.ok:
          stored = true;
          return _json({}, 200);
        case _Put.unauthorized:
          return _json({'message': 'signature expired'}, 401);
        case _Put.forbidden:
          return _json({'message': 'signature rejected'}, 403);
        case _Put.lostAfterStore:
          stored = true;
          throw http.ClientException('connection dropped');
        case _Put.okButMissing:
          return _json({}, 200);
      }
    }
    final path = request.url.path.replaceAll(
      RegExp(r'/records/[^/]+'),
      '/records/{id}',
    );
    calls.add('${request.method} $path');
    if (request.method == 'POST' && path == '/records') {
      final json = jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
      final id = json['record_id'] as String;
      reservationIds.add(id);
      reservations++;
      final upload = uploads.isEmpty
          ? _signed(reservations)
          : uploads.removeAt(0);
      return _json({
        ...recordJson(id),
        if (!identical(upload, _absent)) 'upload': upload,
      }, reservations == 1 ? 201 : 200);
    }
    final id = reservationIds.last;
    if (request.method == 'GET') {
      return _json(recordJson(id, status: status), 200);
    }
    if (path == '/records/{id}/uploaded') {
      if (!stored) {
        return _json({
          'error': {
            'code': 'UPLOAD_MISSING',
            'message': '원본이 없어요.',
            'retryable': true,
          },
        }, 409);
      }
      status = 'uploaded';
      return _json(recordJson(id, status: status), 200);
    }
    return _json({}, 404);
  });

  static http.StreamedResponse _json(Object body, int status) =>
      http.StreamedResponse(
        Stream.value(utf8.encode(jsonEncode(body))),
        status,
      );
}

const _absent = Object();

void main() {
  late ApiTestAuth auth;
  late _Server server;
  late DateTime now;

  setUp(() async {
    auth = ApiTestAuth();
    await auth.signIn('preview.a@example.com', PreviewAuthService.password);
    server = _Server();
    now = _now;
  });
  tearDown(() {
    server.client.close();
    auth.dispose();
  });

  ApiRecordService service() => ApiRecordService(
    client: server.client,
    baseUrl: Uri.parse('https://api.invalid'),
    auth: auth,
    personaId: 'persona',
    policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
    fileTransport: HttpRecordFileTransport(client: server.client),
    clock: () => now,
  );

  RecordUpload upload() => RecordUpload(
    file: file('synthetic.txt'),
    consentAt: _now,
    consentVersion: 'test',
  );

  test('sends the original to the signed URL without the app token', () async {
    final attempt = upload();
    await service().upload(attempt, () {});
    expect(server.calls, [
      'POST /records',
      'PUT /signed/1',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
    ]);
    final put = server.storageRequests.single;
    expect(put.method, 'PUT');
    expect(put.headers.containsKey('authorization'), isFalse);
    expect(put.headers['x-upsert'], 'false');
    expect(put.headers['content-type'], 'text/plain');
    expect(put.contentLength, 128);
    expect(attempt.stage, UploadStage.done);
  });

  test('an expiring signature is renewed with the same ID first', () async {
    server.uploads.addAll([
      _signed(1, valid: const Duration(seconds: 10)),
      _signed(2),
    ]);
    final attempt = upload();
    await service().upload(attempt, () {});
    expect(server.calls, [
      'POST /records',
      'GET /records/{id}',
      'POST /records',
      'PUT /signed/2',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
    ]);
    expect(server.reservationIds, [attempt.id, attempt.id]);
    expect(attempt.stage, UploadStage.done);
  });

  test('a Storage 401 renews the signature and keeps the session', () async {
    server.puts.add(_Put.unauthorized);
    final attempt = upload();
    await service().upload(attempt, () {});
    expect(auth.identity, isNotNull);
    expect(server.calls, [
      'POST /records',
      'PUT /signed/1',
      'GET /records/{id}',
      'POST /records',
      // The refused transfer might have stored the file: ask before resending.
      'POST /records/{id}/uploaded',
      'PUT /signed/2',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
    ]);
    expect(attempt.stage, UploadStage.done);
  });

  test(
    'a second refusal stops with a retryable error and the retry works',
    () async {
      server.puts.addAll([_Put.forbidden, _Put.unauthorized]);
      final attempt = upload();
      final api = service();
      await expectLater(
        api.upload(attempt, () {}),
        throwsA(
          isA<BackendFailure>()
              .having((f) => f.code, 'code', 'STORAGE_REJECTED')
              .having((f) => f.retryable, 'retryable', isTrue)
              .having((f) => f.statusCode, 'statusCode', 401),
        ),
      );
      expect(auth.identity, isNotNull);
      // The retry starts with a fresh signature.
      expect(attempt.stage, UploadStage.reserve);
      server.calls.clear();
      await api.upload(attempt, () {});
      expect(server.calls, [
        'GET /records/{id}',
        'POST /records',
        'POST /records/{id}/uploaded',
        'PUT /signed/3',
        'GET /records/{id}',
        'POST /records/{id}/uploaded',
      ]);
      expect(attempt.stage, UploadStage.done);
    },
  );

  test('a lost Storage answer is checked before sending again', () async {
    server.puts.add(_Put.lostAfterStore);
    final attempt = upload();
    final api = service();
    await expectLater(
      api.upload(attempt, () {}),
      throwsA(
        isA<BackendFailure>().having(
          (f) => f.kind,
          'kind',
          BackendFailureKind.network,
        ),
      ),
    );
    expect(attempt.stage, UploadStage.transfer);
    server.calls.clear();
    await api.upload(attempt, () {});
    // Stored already: no second transfer.
    expect(server.calls, ['POST /records/{id}/uploaded']);
    expect(server.storageRequests, hasLength(1));
    expect(attempt.stage, UploadStage.done);
  });

  test('a missing original at completion is sent again next time', () async {
    server.puts.add(_Put.okButMissing);
    final attempt = upload();
    final api = service();
    await expectLater(
      api.upload(attempt, () {}),
      throwsA(
        isA<BackendFailure>().having((f) => f.code, 'code', 'UPLOAD_MISSING'),
      ),
    );
    expect(attempt.stage, UploadStage.transfer);
    server.calls.clear();
    await api.upload(attempt, () {});
    expect(server.calls, [
      'PUT /signed/1',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
    ]);
    expect(attempt.stage, UploadStage.done);
  });

  test('upload: null means the original is stored and is not sent', () async {
    server.uploads.add(null);
    server.stored = true;
    final attempt = upload();
    await service().upload(attempt, () {});
    expect(server.calls, [
      'POST /records',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
    ]);
    expect(server.storageRequests, isEmpty);
    expect(attempt.stage, UploadStage.done);
  });

  test('a reservation without signed upload information is not sent', () async {
    server.uploads.add(_absent);
    final attempt = upload();
    await expectLater(service().upload(attempt, () {}), throwsStateError);
    expect(server.storageRequests, isEmpty);
    expect(auth.identity, isNotNull);
  });

  group('RecordUploadTarget.fromJson', () {
    test('reads the contract fields', () {
      final target = RecordUploadTarget.fromJson({
        'url': 'https://storage.invalid/signed/1?token=x',
        'method': 'put',
        'headers': {'Content-Type': 'audio/mp4'},
        'expires_at': '2026-11-27T09:00:00Z',
      });
      expect(target.url.host, 'storage.invalid');
      expect(target.method, 'PUT');
      expect(target.headers, {'Content-Type': 'audio/mp4'});
      expect(target.expiresAt, DateTime.utc(2026, 11, 27, 9));
      expect(target.expiredAt(DateTime.utc(2026, 11, 27, 8, 59)), isFalse);
      expect(
        target.expiredAt(
          DateTime.utc(2026, 11, 27, 8, 59, 40),
          margin: const Duration(seconds: 30),
        ),
        isTrue,
      );
    });

    test('rejects malformed values', () {
      final valid = {
        'url': 'https://storage.invalid/a',
        'method': 'PUT',
        'headers': <String, String>{},
        'expires_at': '2026-11-27T09:00:00Z',
      };
      for (final broken in <Object?>[
        null,
        'https://storage.invalid/a',
        {...valid, 'url': '/relative'},
        {...valid, 'url': 42},
        {...valid, 'method': ''},
        {
          ...valid,
          'headers': {'x': 1},
        },
        {...valid}..remove('expires_at'),
      ]) {
        expect(
          () => RecordUploadTarget.fromJson(broken),
          throwsFormatException,
          reason: '$broken',
        );
      }
    });
  });
}
