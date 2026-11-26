import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/records/api_record_service.dart';
import 'package:mobile/records/http_record_transport.dart';
import 'package:mobile/records/record_controller.dart';
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

  /// Stores an object whose size differs from the reservation.
  wrongSize,
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

Map<String, Object?> _error(String code, {required bool retryable}) => {
  'error': {'code': code, 'message': '합성 오류', 'retryable': retryable},
};

/// A synthetic backend and Storage following the app-backend contract.
class _Server {
  final calls = <String>[];
  final reservationIds = <String>[];
  final deletedIds = <String>[];
  final storageRequests = <http.BaseRequest>[];
  final puts = <_Put>[];

  /// The `upload` value of each reservation in turn; a fresh signature when
  /// the list runs out. Use [_absent] to leave the key out.
  final uploads = <Object?>[];

  /// Treats every new record as already having its original.
  var storedOnReserve = false;

  /// Runs after each `PUT` reaches Storage.
  void Function()? afterPut;

  final _status = <String, String>{};
  final _stored = <String>{};
  final _wrongSize = <String>{};
  final _signedFor = <String, String>{};
  var reservations = 0;

  late final client = MockClient.streaming((request, body) async {
    final bytes = await body.toBytes();
    if (request.url.host == 'storage.invalid') {
      calls.add('PUT ${request.url.path}');
      storageRequests.add(request);
      final id = _signedFor[request.url.path]!;
      final put = puts.isEmpty ? _Put.ok : puts.removeAt(0);
      afterPut?.call();
      switch (put) {
        case _Put.ok:
          _stored.add(id);
          return _json({}, 200);
        case _Put.unauthorized:
          return _json({'message': 'signature expired'}, 401);
        case _Put.forbidden:
          return _json({'message': 'signature rejected'}, 403);
        case _Put.lostAfterStore:
          _stored.add(id);
          throw http.ClientException('connection dropped');
        case _Put.okButMissing:
          return _json({}, 200);
        case _Put.wrongSize:
          _stored.add(id);
          _wrongSize.add(id);
          return _json({}, 200);
      }
    }
    final match = RegExp(r'^/records/([^/]+)').firstMatch(request.url.path);
    final id = match?.group(1);
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
      _status.putIfAbsent(id, () => 'uploading');
      if (storedOnReserve) _stored.add(id);
      final upload = uploads.isEmpty
          ? _signed(reservations)
          : uploads.removeAt(0);
      if (upload is Map) {
        _signedFor[Uri.parse(upload['url'] as String).path] = id;
      }
      return _json({
        ...recordJson(id),
        if (!identical(upload, _absent)) 'upload': upload,
      }, reservations == 1 ? 201 : 200);
    }
    if (request.method == 'GET' && path == '/records') {
      return _json({
        'items': [
          for (final e in _status.entries)
            if (e.value != 'deleting') recordJson(e.key, status: e.value),
        ],
      }, 200);
    }
    if (request.url.path.startsWith('/deletion-jobs')) {
      final jobs = [for (final id in deletedIds) _job(id)];
      return _json(
        request.url.path == '/deletion-jobs' ? {'items': jobs} : jobs.last,
        200,
      );
    }
    if (id == null || !_status.containsKey(id)) return _json({}, 404);
    if (request.method == 'GET') {
      return _json(recordJson(id, status: _status[id]!), 200);
    }
    if (request.method == 'DELETE') {
      deletedIds.add(id);
      _status[id] = 'deleting';
      return _json(_job(id), 202);
    }
    if (path == '/records/{id}/uploaded') {
      if (!_stored.contains(id)) {
        return _json(_error('UPLOAD_MISSING', retryable: true), 409);
      }
      if (_wrongSize.contains(id)) {
        return _json(_error('UPLOAD_SIZE_MISMATCH', retryable: false), 409);
      }
      _status[id] = 'uploaded';
      return _json(recordJson(id, status: 'uploaded'), 200);
    }
    return _json({}, 404);
  });

  static Map<String, Object?> _job(String id) => {
    'id': 'job-$id',
    'target_id': id,
    'target_type': 'record',
    'status': 'completed',
    'remaining_steps': <String>[],
  };

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

  test('a missing original at completion is sent again at once', () async {
    server.puts.add(_Put.okButMissing);
    final attempt = upload();
    await service().upload(attempt, () {});
    expect(server.calls, [
      'POST /records',
      'PUT /signed/1',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
      // 409 UPLOAD_MISSING: the same signature is still valid.
      'PUT /signed/1',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
    ]);
    expect(attempt.stage, UploadStage.done);
  });

  test('a second missing original stops and the retry sends it', () async {
    server.puts.addAll([_Put.okButMissing, _Put.okButMissing]);
    final attempt = upload();
    final api = service();
    await expectLater(
      api.upload(attempt, () {}),
      throwsA(
        isA<BackendFailure>()
            .having((f) => f.code, 'code', 'UPLOAD_MISSING')
            .having((f) => f.retryable, 'retryable', isTrue),
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

  test('a missing original after upload: null gets a new signature', () async {
    // The reservation said the original was stored, but it is not.
    server.uploads.add(null);
    final attempt = upload();
    await service().upload(attempt, () {});
    expect(server.calls, [
      'POST /records',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
      'GET /records/{id}',
      'POST /records',
      'PUT /signed/2',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
    ]);
    expect(attempt.stage, UploadStage.done);
  });

  test('a size mismatch is left to the caller with the record kept', () async {
    server.puts.add(_Put.wrongSize);
    final attempt = upload();
    await expectLater(
      service().upload(attempt, () {}),
      throwsA(
        isA<BackendFailure>()
            .having((f) => f.code, 'code', 'UPLOAD_SIZE_MISMATCH')
            .having((f) => f.retryable, 'retryable', isFalse),
      ),
    );
    expect(server.deletedIds, isEmpty);
    expect(attempt.stage, UploadStage.complete);
  });

  test('upload: null means the original is stored and is not sent', () async {
    server.uploads.add(null);
    server.storedOnReserve = true;
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

  group('size mismatch at completion', () {
    RecordController controller({RecordFile? picked}) {
      final c = RecordController(service());
      addTearDown(c.dispose);
      c.select(picked ?? file('synthetic.txt'));
      c.setConsent(true);
      return c;
    }

    test(
      'deletes the record and sends the same file as a new record',
      () async {
        server.puts.add(_Put.wrongSize);
        final c = controller();
        await c.send();
        expect(c.uploadSucceeded, isTrue);
        expect(c.uploadError, isNull);
        expect(server.reservationIds.toSet(), hasLength(2));
        final [first, second] = server.reservationIds.toSet().toList();
        expect(server.deletedIds, [first]);
        expect(c.attempt!.id, second);
        // Same consent and file, new record ID.
        expect(c.attempt!.file.name, 'synthetic.txt');
        expect(c.deletions.keys, contains(first));
        expect(c.uploadNotice, contains('새 기록으로 다시 올렸어요'));
        expect(auth.identity, isNotNull);
      },
    );

    test('a file that changed since the reservation is picked again', () async {
      var size = 128;
      final picked = RecordFile(
        name: 'synthetic.txt',
        sizeBytes: 128,
        openRead: () => Stream.value(List.filled(size, 65)),
      );
      server.puts.add(_Put.wrongSize);
      server.afterPut = () => size = 100;
      final c = controller(picked: picked);
      await c.send();
      expect(c.uploadSucceeded, isFalse);
      expect(server.deletedIds, hasLength(1));
      expect(server.reservations, 1);
      expect(c.attempt, isNull);
      expect(c.uploadRetryAllowed, isFalse);
      expect(c.uploadError, contains('파일이 바뀌었어요'));
    });

    test('a second mismatch stops and asks for the file again', () async {
      server.puts.addAll([_Put.wrongSize, _Put.wrongSize]);
      final c = controller();
      await c.send();
      expect(c.uploadSucceeded, isFalse);
      expect(server.deletedIds, hasLength(2));
      expect(server.reservationIds.toSet(), hasLength(2));
      expect(c.attempt, isNull);
      expect(c.uploadRetryAllowed, isFalse);
      expect(c.uploadError, contains('크기가 맞지 않아'));
    });
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
