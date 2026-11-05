import 'dart:async';
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
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/record_upload_screen.dart';
import 'package:mobile/services/backend_service.dart';

import 'records_test.dart' show ApiTestAuth, file, recordJson;

class _Discarding extends PreviewRecordService {
  final discarded = <RecordUpload>[];
  @override
  Future<void> discardUpload(RecordUpload upload) async =>
      discarded.add(upload);
}

RecordUpload _upload(RecordFile f) => RecordUpload(
  file: f,
  consentAt: DateTime.utc(2026, 11, 1),
  consentVersion: 'test',
);

RecordFile _chunks(List<List<int>> chunks, {int? size, Duration? gap}) {
  final total = size ?? chunks.fold<int>(0, (n, c) => n + c.length);
  return RecordFile(
    name: 'voice.m4a',
    sizeBytes: total,
    openRead: () async* {
      for (final chunk in chunks) {
        if (gap != null) await Future<void>.delayed(gap);
        yield chunk;
      }
    },
  );
}

Future<ApiTestAuth> _auth() async {
  final auth = ApiTestAuth();
  await auth.signIn('preview.a@example.com', PreviewAuthService.password);
  return auth;
}

void main() {
  group('preview upload progress and cancel', () {
    test('progress rises monotonically to the file size', () async {
      final service = PreviewRecordService();
      final upload = _upload(file('voice.m4a', 700));
      final seen = <int>[];
      upload.onProgress = () => seen.add(upload.bytesSent);
      await service.upload(upload, () {});
      expect(seen.first, 0);
      expect(seen.last, 700);
      expect([
        for (var i = 1; i < seen.length; i++) seen[i] >= seen[i - 1],
      ], everyElement(isTrue));
      expect(upload.progress, 1.0);
      expect(upload.stage, UploadStage.done);
    });

    test('cancelling during the transfer stops it and cleans up', () async {
      final service = _Discarding();
      final controller = RecordController(service);
      addTearDown(controller.dispose);
      controller.select(file('voice.m4a', 700));
      controller.setConsent(true);
      final sending = controller.send();
      while (controller.attempt?.stage != UploadStage.transfer) {
        await Future<void>.delayed(const Duration(milliseconds: 10));
      }
      expect(controller.canCancelUpload, isTrue);
      final firstId = controller.attempt!.id;
      controller.cancelUpload();
      expect(controller.cancelling, isTrue);
      await sending;
      expect(controller.uploading, isFalse);
      expect(controller.uploadSucceeded, isFalse);
      expect(controller.uploadNotice, contains('취소'));
      expect(service.discarded.single.id, firstId);
      expect(controller.entries, isEmpty);
      expect(await service.list(), isEmpty);
      // The picked file stays; consent must be given again for a new attempt.
      expect(controller.selected, isNotNull);
      expect(controller.consent, isFalse);
      expect(controller.attempt, isNull);
      expect(controller.canUpload, isFalse);
      controller.setConsent(true);
      await controller.send();
      expect(controller.uploadSucceeded, isTrue);
      expect(controller.entries.length, 1);
      expect(controller.entries.single.id, isNot(firstId));
    });

    test('cancelling is refused once the file is fully transferred', () async {
      final controller = RecordController(PreviewRecordService());
      addTearDown(controller.dispose);
      controller.select(file('voice.m4a', 700));
      controller.setConsent(true);
      final sending = controller.send();
      while (controller.attempt?.stage != UploadStage.complete) {
        await Future<void>.delayed(const Duration(milliseconds: 10));
      }
      expect(controller.canCancelUpload, isFalse);
      controller.cancelUpload();
      expect(controller.cancelling, isFalse);
      await sending;
      expect(controller.uploadSucceeded, isTrue);
    });

    testWidgets('upload screen shows percent progress and cancel', (
      tester,
    ) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(800, 1600);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      final controller = RecordController(_Discarding());
      addTearDown(controller.dispose);
      controller.select(file('voice.m4a', 7000));
      controller.setConsent(true);
      await tester.pumpWidget(
        MaterialApp(
          home: RecordUploadScreen(
            controller: controller,
            picker: () async => null,
          ),
        ),
      );
      await tester.tap(find.byKey(const ValueKey('record-upload')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pump(const Duration(milliseconds: 120));
      expect(find.textContaining('파일 올리는 중 · '), findsOneWidget);
      expect(find.textContaining('%'), findsWidgets);
      final bar = tester.widget<LinearProgressIndicator>(
        find.byKey(const ValueKey('record-upload-progress')),
      );
      expect(bar.value, isNotNull);
      await tester.tap(find.byKey(const ValueKey('record-upload-cancel')));
      await tester.pump();
      await tester.pumpAndSettle();
      expect(find.textContaining('업로드를 취소했어요'), findsOneWidget);
      expect(find.byKey(const ValueKey('record-upload-cancel')), findsNothing);
      expect(controller.entries, isEmpty);
    });
  });

  group('HttpRecordFileTransport', () {
    final target = RecordUploadTarget(
      url: Uri.parse('https://storage.invalid/replica/original'),
      headers: const {'x-agreed': 'yes'},
    );

    test('streams every byte with progress and agreed headers', () async {
      late http.BaseRequest seen;
      final body = <int>[];
      final client = MockClient.streaming((request, stream) async {
        seen = request;
        body.addAll(await stream.toBytes());
        return http.StreamedResponse(const Stream.empty(), 200);
      });
      final transport = HttpRecordFileTransport(
        client: client,
        resolveTarget: (_) async => target,
      );
      final upload = _upload(
        _chunks([
          [1, 2],
          [3, 4],
          [5, 6],
        ]),
      );
      final progress = <int>[];
      upload.onProgress = () => progress.add(upload.bytesSent);
      await transport.ensureUploaded(upload);
      expect(body, [1, 2, 3, 4, 5, 6]);
      expect(seen.method, 'PUT');
      expect(seen.url, target.url);
      expect(seen.headers['x-agreed'], 'yes');
      expect(seen.headers['content-type'], 'audio/mp4');
      expect(seen.contentLength, 6);
      expect(progress, [0, 2, 4, 6]);
      expect(upload.progress, 1.0);
    });

    test('maps a rejected upload to a typed failure', () async {
      final client = MockClient.streaming((request, stream) async {
        await stream.drain<void>();
        return http.StreamedResponse(const Stream.empty(), 403);
      });
      final transport = HttpRecordFileTransport(
        client: client,
        resolveTarget: (_) async => target,
      );
      await expectLater(
        transport.ensureUploaded(
          _upload(
            _chunks([
              [1],
            ]),
          ),
        ),
        throwsA(
          isA<BackendFailure>().having(
            (f) => f.kind,
            'kind',
            BackendFailureKind.forbidden,
          ),
        ),
      );
    });

    test(
      'cancel while streaming stops reading and reports cancelled',
      () async {
        var delivered = 0;
        final client = MockClient.streaming((request, stream) async {
          await stream.drain<void>();
          return http.StreamedResponse(const Stream.empty(), 200);
        });
        final transport = HttpRecordFileTransport(
          client: client,
          resolveTarget: (_) async => target,
        );
        final upload = _upload(
          _chunks([
            [1, 2],
            [3, 4],
            [5, 6],
          ], gap: const Duration(milliseconds: 5)),
        );
        upload.onProgress = () {
          delivered = upload.bytesSent;
          if (delivered == 2) upload.cancelToken.cancel();
        };
        await expectLater(
          transport.ensureUploaded(upload),
          throwsA(isA<UploadCancelled>()),
        );
        expect(delivered, 2);
      },
    );

    test(
      'a file that changed after it was picked is never sent short',
      () async {
        final client = MockClient.streaming((request, stream) async {
          await stream.drain<void>();
          return http.StreamedResponse(const Stream.empty(), 200);
        });
        final transport = HttpRecordFileTransport(
          client: client,
          resolveTarget: (_) async => target,
        );
        for (final size in [4, 8]) {
          await expectLater(
            transport.ensureUploaded(
              _upload(
                _chunks([
                  [1, 2, 3],
                  [4, 5, 6],
                ], size: size),
              ),
            ),
            throwsA(
              isA<BackendFailure>().having(
                (f) => f.code,
                'code',
                'FILE_CHANGED',
              ),
            ),
          );
        }
      },
    );

    test('an original that is already stored is not sent again', () async {
      var requests = 0;
      final client = MockClient.streaming((request, stream) async {
        requests++;
        return http.StreamedResponse(const Stream.empty(), 200);
      });
      final transport = HttpRecordFileTransport(
        client: client,
        resolveTarget: (_) async => target,
        alreadyStored: (_) async => true,
      );
      final upload = _upload(
        _chunks([
          [1, 2],
        ]),
      );
      await transport.ensureUploaded(upload);
      expect(requests, 0);
      expect(upload.progress, 1.0);
    });
  });

  group('ApiRecordService cancel handling', () {
    ApiRecordService service(http.Client client, ApiTestAuth auth) =>
        ApiRecordService(
          client: client,
          baseUrl: Uri.parse('https://example.invalid'),
          auth: auth,
          personaId: 'persona',
          policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
          fileTransport: _CancelOnTransfer(),
        );

    test('discard removes a reservation that has no file yet', () async {
      final auth = await _auth();
      addTearDown(auth.dispose);
      final calls = <String>[];
      late String id;
      final client = MockClient((request) async {
        calls.add('${request.method} ${request.url.path}');
        if (request.method == 'DELETE') {
          return http.Response(
            jsonEncode({
              'id': 'job',
              'target_id': id,
              'target_type': 'record',
              'status': 'pending',
              'remaining_steps': ['storage', 'database'],
            }),
            202,
          );
        }
        return http.Response(jsonEncode(recordJson(id)), 200);
      });
      addTearDown(client.close);
      final api = service(client, auth);
      final upload = _upload(file('a.txt'))..reservationRequested = true;
      id = upload.id;
      await api.discardUpload(upload);
      expect(calls, ['GET /records/$id', 'DELETE /records/$id']);
    });

    test('discard keeps a finished upload and ignores a missing one', () async {
      final auth = await _auth();
      addTearDown(auth.dispose);
      var status = 'uploaded';
      var missing = false;
      final calls = <String>[];
      late String id;
      final client = MockClient((request) async {
        calls.add(request.method);
        if (missing) return http.Response('{}', 404);
        return http.Response(jsonEncode(recordJson(id, status: status)), 200);
      });
      addTearDown(client.close);
      final api = service(client, auth);
      final upload = _upload(file('a.txt'))..reservationRequested = true;
      id = upload.id;
      await api.discardUpload(upload);
      missing = true;
      await api.discardUpload(upload);
      expect(calls, ['GET', 'GET']);
      // Never reserved: nothing to look up at all.
      calls.clear();
      await api.discardUpload(_upload(file('b.txt')));
      expect(calls, isEmpty);
    });

    test('a cancelled upload sends no further request', () async {
      final auth = await _auth();
      addTearDown(auth.dispose);
      var requests = 0;
      final client = MockClient((request) async {
        requests++;
        return http.Response('{}', 500);
      });
      addTearDown(client.close);
      final api = service(client, auth);
      final upload = _upload(file('a.txt'))..cancelToken.cancel();
      await expectLater(
        api.upload(upload, () {}),
        throwsA(isA<UploadCancelled>()),
      );
      expect(requests, 0);
    });

    test('speaker choices are sent only when the contract allows it', () async {
      final auth = await _auth();
      addTearDown(auth.dispose);
      Future<Map<String, Object?>> reserve(bool allowed) async {
        late Map<String, Object?> body;
        final client = MockClient((request) async {
          if (request.method == 'POST') {
            body = (jsonDecode(request.body) as Map).cast<String, Object?>();
            return http.Response(
              jsonEncode(recordJson(body['record_id']! as String)),
              201,
            );
          }
          return http.Response('{}', 404);
        });
        final api = ApiRecordService(
          client: client,
          baseUrl: Uri.parse('https://example.invalid'),
          auth: auth,
          personaId: 'persona',
          policy: RecordPolicy(
            maxBytes: 1000,
            consentVersion: 'test',
            sendSpeakerSelection: allowed,
          ),
          fileTransport: _CancelOnTransfer(),
        );
        final upload = RecordUpload(
          file: file('a.txt'),
          consentAt: DateTime.utc(2026, 11, 1),
          consentVersion: 'test',
          targetSpeaker: '엄마',
          providerSpeaker: '나',
        );
        try {
          await api.upload(upload, () {});
        } on UploadCancelled {
          /* The transport stops the flow after the reservation. */
        }
        client.close();
        return body;
      }

      final off = await reserve(false);
      expect(off.containsKey('target_speaker'), isFalse);
      expect(off.containsKey('provider_speaker'), isFalse);
      final on = await reserve(true);
      expect(on['target_speaker'], '엄마');
      expect(on['provider_speaker'], '나');
      expect(on.containsKey('user_id'), isFalse);
    });
  });
}

class _CancelOnTransfer extends RecordFileTransport {
  @override
  Future<void> ensureUploaded(RecordUpload upload) async {
    upload.cancelToken.cancel();
    throw const UploadCancelled();
  }
}
