import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/records/api_record_service.dart';
import 'package:mobile/records/http_record_transport.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_upload_screen.dart';
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

Map<String, Object?> _error(
  String code, {
  required bool retryable,
  String? field,
  List<String>? speakers,
}) => {
  'error': {
    'code': code,
    'message': '합성 오류',
    'retryable': retryable,
    'field': ?field,
    'speakers': ?speakers,
  },
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

  /// When set, `/uploaded` checks the final speaker values as the contract's
  /// '업로드 뒤 검증' does. Null means the server does not see a chat.
  var checkSpeakers = false;
  List<String>? fileSpeakers;

  /// A 422 answer for the next reservation or `/uploaded`, if set.
  Map<String, Object?>? refuseReservation;
  Map<String, Object?>? refuseUploaded;

  /// The record disappears once its original is in Storage (GET answers 404).
  var forgetAfterPut = false;

  /// The server fails the record once its original is in Storage.
  var failAfterPut = false;

  /// Marks the latest record as failed by the server.
  void markFailed() => _status[reservationIds.last] = 'failed';

  /// Bodies `/uploaded` received, in order (an empty map when none was sent).
  final uploadedBodies = <Map<String, Object?>>[];
  final _target = <String, String?>{};
  final _provider = <String, String?>{};

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
      if (forgetAfterPut) _status.remove(id);
      if (failAfterPut) _status[id] = 'failed';
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
      final refused = refuseReservation;
      if (refused != null) {
        refuseReservation = null;
        return _json(refused, 422);
      }
      final json = jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
      final id = json['record_id'] as String;
      reservationIds.add(id);
      reservations++;
      _status.putIfAbsent(id, () => 'uploading');
      // A reservation's speaker values replace the earlier ones.
      _target[id] = json['target_speaker'] as String?;
      _provider[id] = json['provider_speaker'] as String?;
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
      if (_status[id] == 'failed') {
        return _json(_error('CONFLICT', retryable: false), 409);
      }
      if (!_stored.contains(id)) {
        return _json(_error('UPLOAD_MISSING', retryable: true), 409);
      }
      if (_wrongSize.contains(id)) {
        return _json(_error('UPLOAD_SIZE_MISMATCH', retryable: false), 409);
      }
      final refusedUpload = refuseUploaded;
      if (refusedUpload != null) {
        refuseUploaded = null;
        return _json(refusedUpload, 422);
      }
      final sent = bytes.isEmpty
          ? <String, Object?>{}
          : jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
      uploadedBodies.add(sent);
      // Leaving a key out keeps the reserved value; null clears it.
      if (sent.containsKey('target_speaker')) {
        _target[id] = sent['target_speaker'] as String?;
      }
      if (sent.containsKey('provider_speaker')) {
        _provider[id] = sent['provider_speaker'] as String?;
      }
      final refused = checkSpeakers ? _speakerError(id) : null;
      if (refused != null) return _json(refused, 422);
      _status[id] = 'uploaded';
      return _json(recordJson(id, status: 'uploaded'), 200);
    }
    return _json({}, 404);
  });

  Map<String, Object?>? _speakerError(String id) {
    final names = fileSpeakers;
    final target = _target[id], provider = _provider[id];
    Map<String, Object?> refuse(String code, String field) => _error(
      code,
      retryable: true,
      field: field,
      speakers: names ?? const [],
    );
    if (names == null) {
      return target != null || provider != null
          ? refuse('SPEAKER_NOT_ALLOWED', 'target_speaker')
          : null;
    }
    if (names.length == 1 && provider != null) {
      return refuse('SPEAKER_NOT_ALLOWED', 'provider_speaker');
    }
    if (target == null) return refuse('SPEAKER_REQUIRED', 'target_speaker');
    if (!names.contains(target)) {
      return refuse('SPEAKER_NOT_FOUND', 'target_speaker');
    }
    if (names.length >= 2) {
      if (provider == null) {
        return refuse('SPEAKER_REQUIRED', 'provider_speaker');
      }
      if (!names.contains(provider)) {
        return refuse('SPEAKER_NOT_FOUND', 'provider_speaker');
      }
    }
    return null;
  }

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
        headers: {'content-type': 'application/json'},
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

  test('a device clock running ahead does not block the upload', () async {
    // Every signature looks expired to a clock two hours ahead.
    now = _now.add(const Duration(hours: 2));
    final attempt = upload();
    await service().upload(attempt, () {});
    expect(server.calls, [
      'POST /records',
      // Renewed once, then sent anyway: Storage decides.
      'GET /records/{id}',
      'POST /records',
      'PUT /signed/2',
      'GET /records/{id}',
      'POST /records/{id}/uploaded',
    ]);
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

  group('speaker errors after the upload', () {
    RecordFile chat(String body) {
      final bytes = utf8.encode(body);
      return RecordFile(
        name: 'kakao.txt',
        sizeBytes: bytes.length,
        openRead: () => Stream.value(bytes),
      );
    }

    const pair = '[엄마] [오후 3:12] 밥은 먹었니?\n[나] [오후 3:13] 네\n';

    Future<RecordController> picked(RecordFile picked) async {
      final c = RecordController(service());
      addTearDown(c.dispose);
      c.select(picked);
      c.setConsent(true);
      await pumpEventQueue();
      return c;
    }

    test(
      'the service keeps the record at completion for a new choice',
      () async {
        server
          ..checkSpeakers = true
          ..fileSpeakers = ['엄마', '나'];
        final attempt = upload();
        final api = service();
        await expectLater(
          api.upload(attempt, () {}),
          throwsA(
            isA<BackendFailure>()
                .having((f) => f.code, 'code', 'SPEAKER_REQUIRED')
                .having((f) => f.field, 'field', 'target_speaker')
                .having((f) => f.speakers, 'speakers', ['엄마', '나']),
          ),
        );
        expect(attempt.stage, UploadStage.complete);
        attempt
          ..targetSpeaker = '엄마'
          ..providerSpeaker = '나'
          ..speakersChanged = true;
        server.calls.clear();
        await api.upload(attempt, () {});
        // The file is not sent again.
        expect(server.calls, [
          'GET /records/{id}',
          'POST /records/{id}/uploaded',
        ]);
        expect(server.uploadedBodies.last, {
          'target_speaker': '엄마',
          'provider_speaker': '나',
        });
        expect(attempt.stage, UploadStage.done);
      },
    );

    test(
      'the user chooses from the server list and only /uploaded is called',
      () async {
        server
          ..checkSpeakers = true
          ..fileSpeakers = ['엄마', '나'];
        // The device read the file as plain text.
        final c = await picked(file('synthetic.txt'));
        expect(c.needsSpeakers, isFalse);
        await c.send();
        expect(c.uploadSucceeded, isFalse);
        expect(c.speakerRecovery, isTrue);
        expect(c.uploadError, contains('다시 골라'));
        expect(c.analysis!.speakers.map((s) => s.name), ['엄마', '나']);
        expect(c.canUpload, isFalse);
        c.selectTarget('엄마');
        c.selectProvider('나');
        expect(c.canUpload, isTrue);
        await c.send();
        expect(c.uploadSucceeded, isTrue);
        expect(c.speakerRecovery, isFalse);
        expect(server.storageRequests, hasLength(1));
        expect(server.uploadedBodies.last, {
          'target_speaker': '엄마',
          'provider_speaker': '나',
        });
      },
    );

    test('a lone speaker from the server still needs the user tap', () async {
      server
        ..checkSpeakers = true
        ..fileSpeakers = ['엄마'];
      final c = await picked(file('synthetic.txt'));
      await c.send();
      expect(c.speakerRecovery, isTrue);
      expect(c.singleSpeaker, isTrue);
      expect(c.targetSpeaker, isNull);
      expect(c.canUpload, isFalse);
      c.selectTarget('엄마');
      await c.send();
      expect(c.uploadSucceeded, isTrue);
      // An explicit null leaves no provider for a one-speaker file.
      expect(server.uploadedBodies.last, {
        'target_speaker': '엄마',
        'provider_speaker': null,
      });
    });

    test('values the server does not allow are cleared at once', () async {
      server.checkSpeakers = true; // Not a chat for the server.
      final c = await picked(chat(pair));
      c.selectTarget('엄마');
      c.selectProvider('나');
      await c.send();
      expect(c.uploadSucceeded, isTrue);
      expect(c.speakerRecovery, isFalse);
      expect(server.uploadedBodies, [
        <String, Object?>{},
        {'target_speaker': null, 'provider_speaker': null},
      ]);
    });

    test('a provider for a one-speaker file is cleared at once', () async {
      server
        ..checkSpeakers = true
        ..fileSpeakers = ['엄마'];
      final c = await picked(chat(pair));
      c.selectTarget('엄마');
      c.selectProvider('나');
      await c.send();
      expect(c.uploadSucceeded, isTrue);
      expect(server.uploadedBodies.last, {
        'target_speaker': '엄마',
        'provider_speaker': null,
      });
    });

    test('after clearing, a missing speaker is chosen again', () async {
      server
        ..checkSpeakers = true
        ..fileSpeakers = ['아빠'];
      final c = await picked(chat(pair));
      c.selectTarget('엄마');
      c.selectProvider('나');
      await c.send();
      expect(c.uploadSucceeded, isFalse);
      expect(c.speakerRecovery, isTrue);
      expect(c.analysis!.speakers.map((s) => s.name), ['아빠']);
      // '엄마' is not in the server's list any more.
      expect(c.targetSpeaker, isNull);
      expect(c.providerSpeaker, isNull);
    });

    test('a new file leaves the speaker choice behind', () async {
      server
        ..checkSpeakers = true
        ..fileSpeakers = ['엄마', '나'];
      final c = await picked(file('synthetic.txt'));
      await c.send();
      expect(c.speakerRecovery, isTrue);
      final first = c.attempt!.id;
      c.beginSelection();
      await pumpEventQueue();
      expect(c.speakerRecovery, isFalse);
      expect(c.analysis, isNull);
      // The waiting record is cleaned up.
      expect(server.deletedIds, [first]);
    });

    testWidgets('SC-04 shows the server speakers to choose again', (
      tester,
    ) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(800, 2000);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      server
        ..checkSpeakers = true
        ..fileSpeakers = ['엄마', '나'];
      final c = RecordController(service());
      addTearDown(c.dispose);
      await tester.runAsync(() async {
        c.select(file('synthetic.txt'));
        c.setConsent(true);
        await pumpEventQueue();
        await c.send();
      });
      await tester.pumpWidget(
        MaterialApp(
          home: RecordUploadScreen(controller: c, picker: () async => null),
        ),
      );
      await tester.pump();
      expect(find.text('누구의 말투를 반영할까요?'), findsOneWidget);
      expect(find.textContaining('다시 골라'), findsOneWidget);
      // The server gives names only, so no message counts are shown.
      expect(find.textContaining('메시지 '), findsNothing);
      FilledButton button() => tester.widget<FilledButton>(
        find.byKey(const ValueKey('record-upload')),
      );
      expect(button().onPressed, isNull);
      expect(find.text('선택한 화자로 이어서 올리기'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('speaker-target-엄마')));
      await tester.pump();
      await tester.tap(find.byKey(const ValueKey('speaker-provider-나')));
      await tester.pump();
      expect(button().onPressed, isNotNull);
      expect(tester.takeException(), isNull);
    });
  });

  group('error messages', () {
    test('BackendFailure keeps field and speakers from the envelope', () {
      final withField = BackendFailure.fromResponse(
        statusCode: 422,
        body: _error('INVALID_INPUT', retryable: false, field: 'size_bytes'),
      );
      expect(withField.code, 'INVALID_INPUT');
      expect(withField.field, 'size_bytes');
      expect(withField.speakers, isNull);
      final plain = BackendFailure.fromResponse(
        statusCode: 422,
        body: _error('INVALID_INPUT', retryable: false),
      );
      expect(plain.field, isNull);
      final speakers = BackendFailure.fromResponse(
        statusCode: 422,
        body: _error(
          'SPEAKER_REQUIRED',
          retryable: true,
          field: 'target_speaker',
          speakers: ['엄마'],
        ),
      );
      expect(speakers.speakers, ['엄마']);
      // A malformed list is dropped rather than half read.
      final broken = BackendFailure.fromResponse(
        statusCode: 422,
        body: {
          'error': {
            'code': 'SPEAKER_REQUIRED',
            'speakers': ['엄마', 3],
          },
        },
      );
      expect(broken.speakers, isNull);
    });

    RecordController picked() {
      final c = RecordController(service());
      addTearDown(c.dispose);
      c.select(file('voice.m4a'));
      c.setConsent(true);
      return c;
    }

    test(
      'a refused file content asks to start again with a new file',
      () async {
        server.refuseUploaded = _error('INVALID_INPUT', retryable: false);
        final c = picked();
        await c.send();
        expect(c.uploadSucceeded, isFalse);
        expect(c.uploadError, contains('파일 내용을 처리하지 못했어요'));
        expect(c.uploadError, contains('새 파일로 시작'));
        expect(c.uploadRetryAllowed, isFalse);
        final id = c.attempt!.id;
        // Starting again removes the record that waited.
        c.beginSelection();
        await pumpEventQueue();
        expect(server.deletedIds, [id]);
      },
    );

    test('a refused field keeps the general message', () async {
      server.refuseReservation = _error(
        'INVALID_INPUT',
        retryable: false,
        field: 'consent_at',
      );
      final c = picked();
      await c.send();
      expect(c.uploadSucceeded, isFalse);
      expect(c.uploadError, isNot(contains('파일 내용')));
    });

    test('a record the server failed is not shown as saved', () async {
      server.failAfterPut = true;
      final c = picked();
      await c.send();
      expect(c.uploadSucceeded, isFalse);
      expect(c.uploadError, contains('처리하지 못했어요'));
      expect(c.uploadRetryAllowed, isFalse);
      // Only the completion check ran; /uploaded was not called.
      expect(server.calls.last, 'GET /records/{id}');
    });

    test('a record failed before it is reserved again is not saved', () async {
      server.puts.addAll([_Put.forbidden, _Put.forbidden]);
      final c = picked();
      await c.send();
      expect(c.uploadError, isNotNull);
      expect(c.uploadRetryAllowed, isTrue);
      server.markFailed();
      server.calls.clear();
      await c.send();
      expect(c.uploadSucceeded, isFalse);
      expect(c.uploadError, contains('처리하지 못했어요'));
      // Not reserved again once the lookup shows it failed.
      expect(server.calls, ['GET /records/{id}']);
    });

    test('a record that disappeared is not retried', () async {
      server.forgetAfterPut = true;
      final c = picked();
      await c.send();
      expect(c.uploadSucceeded, isFalse);
      expect(c.uploadError, contains('기록을 찾을 수 없어요'));
      expect(c.uploadError, isNot(contains('다시 시도할 수 있어요')));
      expect(c.uploadRetryAllowed, isFalse);
      c.beginSelection();
      await pumpEventQueue();
      // Nothing is left to delete.
      expect(server.deletedIds, isEmpty);
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
