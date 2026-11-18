import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/main.dart';
import 'package:mobile/records/api_record_service.dart';
import 'package:mobile/records/http_record_transport.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_deletion.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/record_upload_screen.dart';
import 'package:mobile/records/records_panel.dart';
import 'package:mobile/services/backend_service.dart';

import 'records_test.dart' show ApiTestAuth, file, recordJson;

class _Discarding extends PreviewRecordService {
  final discarded = <RecordUpload>[];
  Completer<void>? pendingDiscard;
  DeletionJob? discardedJob;
  DeletionJob? checkedJob;
  bool failDiscard = false;
  @override
  Future<DeletionJob?> discardUpload(RecordUpload upload) async {
    discarded.add(upload);
    await pendingDiscard?.future;
    if (failDiscard) throw StateError('cleanup unavailable');
    return discardedJob;
  }

  @override
  Future<DeletionJob> deletionJob(String jobId) async =>
      checkedJob?.id == jobId ? checkedJob! : await super.deletionJob(jobId);
}

class _TransferringRecords extends _Discarding {
  _TransferringRecords(this.transport);
  final RecordFileTransport transport;

  @override
  Future<void> upload(RecordUpload upload, void Function() changed) async {
    upload.reservationRequested = true;
    upload.stage = UploadStage.transfer;
    changed();
    await transport.ensureUploaded(upload);
    upload.stage = UploadStage.done;
    changed();
  }
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
  group('cancelled upload cleanup', () {
    test('no returned job makes no claim about server deletion', () async {
      final service = _TransferringRecords(_CancelOnTransfer());
      final controller = RecordController(service);
      addTearDown(controller.dispose);
      controller.select(file('voice.m4a'));
      controller.setConsent(true);
      await controller.send();
      expect(controller.uploadNotice, '업로드를 취소했어요.');
      expect(controller.deletions, isEmpty);
    });

    test('abandoning a failed upload also tracks its cleanup job', () async {
      final service = _Discarding();
      final controller = RecordController(service);
      addTearDown(controller.dispose);
      final upload = _upload(file('voice.m4a'))..reservationRequested = true;
      controller.attempt = upload;
      service.discardedJob = DeletionJob(
        id: 'job',
        targetId: upload.id,
        targetType: 'record',
        status: 'pending',
        remaining: ['storage'],
      );
      controller.beginSelection();
      await pumpEventQueue();
      expect(controller.deletions[upload.id]!.job, same(service.discardedJob));
    });

    for (final state in ['pending', 'failed', 'partial', 'completed']) {
      test(
        'cancellation tracks $state without assuming deletion completed',
        () async {
          final pending = Completer<void>();
          final service = _TransferringRecords(_CancelOnTransfer())
            ..pendingDiscard = pending;
          final controller = RecordController(service);
          addTearDown(controller.dispose);
          controller.select(file('voice.m4a'));
          controller.setConsent(true);
          final sending = controller.send();
          await pumpEventQueue();
          expect(controller.uploadNotice, contains('정리하고 있어요'));
          final upload = service.discarded.single;
          service.discardedJob = DeletionJob(
            id: 'cleanup-job',
            targetId: upload.id,
            targetType: 'record',
            status: state == 'partial' ? 'completed' : state,
            remaining: state == 'completed' ? [] : ['storage'],
          );
          pending.complete();
          await sending;
          final deletion = controller.deletions[upload.id]!;
          expect(deletion.job, same(service.discardedJob));
          expect(deletion.label, 'voice.m4a');
          expect(deletion.complete, state == 'completed');
          expect(
            controller.uploadNotice!.contains('임시 기록을 삭제했어요'),
            state == 'completed',
          );
          expect(controller.uploadNotice, isNot(contains('남기지 않았어요')));
          expect(controller.selected, same(upload.file));
          expect(controller.consent, isFalse);
          expect(controller.attempt, isNull);
          if (state == 'pending') {
            expect(controller.uploadNotice, contains('진행 중'));
          } else if (state != 'completed') {
            expect(controller.uploadNotice, contains('삭제하지 못했어요'));
          }
        },
      );
    }

    test('cleanup failures remain accessible from the record list', () async {
      final service = _TransferringRecords(_CancelOnTransfer())
        ..failDiscard = true;
      final controller = RecordController(service);
      addTearDown(controller.dispose);
      controller.select(file('voice.m4a'));
      controller.setConsent(true);
      await controller.send();
      final deletion = controller.deletions.values.single;
      expect(deletion.job, isNull);
      expect(deletion.error, contains('삭제 상태를 확인하지 못했어요'));
      expect(controller.uploadNotice, contains('기록 목록에서 확인'));
      expect(controller.uploadNotice, isNot(contains('삭제했어요')));
    });

    testWidgets(
      'cancelled uploads expose their job for checking failure and completion',
      (tester) async {
        tester.view.devicePixelRatio = 1;
        tester.view.physicalSize = const Size(800, 1600);
        addTearDown(tester.view.resetDevicePixelRatio);
        addTearDown(tester.view.resetPhysicalSize);
        final pending = Completer<void>();
        final service = _TransferringRecords(_CancelOnTransfer())
          ..pendingDiscard = pending;
        final controller = RecordController(service);
        addTearDown(controller.dispose);
        controller.select(file('voice.m4a'));
        controller.setConsent(true);
        final sending = controller.send();
        await tester.pump();
        final upload = service.discarded.single;
        DeletionJob job(String status, List<String> remaining) => DeletionJob(
          id: 'cleanup-job',
          targetId: upload.id,
          targetType: 'record',
          status: status,
          remaining: remaining,
        );
        service.discardedJob = job('pending', ['storage']);
        pending.complete();
        await tester.pump();
        await sending;
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(body: RecordsPanel(controller: controller)),
          ),
        );
        await tester.pumpAndSettle();
        expect(find.text('voice.m4a 삭제 요청'), findsOneWidget);
        expect(find.textContaining('삭제 진행 중'), findsOneWidget);
        service.checkedJob = job('failed', ['storage']);
        await tester.tap(find.text('voice.m4a 삭제 요청'));
        await tester.pumpAndSettle();
        await tester.tap(find.byKey(const ValueKey('delete-confirm')));
        await tester.pumpAndSettle();
        expect(find.text('일부 자료를 삭제하지 못했어요.'), findsOneWidget);
        service.checkedJob = job('completed', []);
        await tester.tap(find.byKey(const ValueKey('delete-confirm')));
        await tester.pumpAndSettle();
        expect(find.text('미리보기 자료 삭제를 완료했어요.'), findsOneWidget);
        expect(controller.deletions[upload.id]!.complete, isTrue);
        expect(service.discarded, hasLength(1));
      },
    );
  });

  group('session-owned uploads', () {
    test(
      'disposing the controller aborts HTTP and releases the file',
      () async {
        final reading = Completer<void>();
        var fileCancelled = false;
        var requestAborted = false;
        final source = StreamController<List<int>>(
          onListen: reading.complete,
          onCancel: () {
            fileCancelled = true;
          },
        );
        final client = MockClient.streaming((request, stream) async {
          final subscription = stream.listen((_) {});
          await (request as http.Abortable).abortTrigger;
          requestAborted = true;
          await subscription.cancel();
          throw http.RequestAbortedException(request.url);
        });
        addTearDown(client.close);
        final service = _TransferringRecords(
          HttpRecordFileTransport(
            client: client,
            resolveTarget: (_) async => RecordUploadTarget(
              url: Uri.parse('https://storage.invalid/original'),
            ),
          ),
        );
        final controller = RecordController(service);
        controller.select(
          RecordFile(
            name: 'voice.m4a',
            sizeBytes: 2,
            openRead: () => source.stream,
          ),
        );
        controller.setConsent(true);
        var notifications = 0;
        controller.addListener(() => notifications++);
        final sending = controller.send();
        await reading.future;
        final upload = controller.attempt!;
        controller.dispose();
        final previousNotifications = notifications;
        expect(upload.cancelToken.isCancelled, isTrue);
        expect(upload.onProgress, isNull);
        await sending;
        expect(requestAborted, isTrue);
        expect(fileCancelled, isTrue);
        expect(controller.attempt, isNull);
        expect(controller.selected, isNull);
        expect(controller.deletions, isEmpty);
        expect(service.discarded, isEmpty);
        source.add([1, 2]);
        upload.reportProgress(2);
        await pumpEventQueue();
        expect(notifications, previousNotifications);
        await source.close();
      },
    );

    testWidgets(
      'logout stops the old upload and a new account stays signed in',
      (tester) async {
        final transfer = _WaitingTransfer();
        final service = _TransferringRecords(transfer);
        final auth = PreviewAuthService();
        addTearDown(auth.dispose);
        await auth.signIn('preview.a@example.com', PreviewAuthService.password);
        var services = 0;
        await tester.pumpWidget(
          ReplicaApp(
            authService: auth,
            recordServiceFactory: () =>
                services++ == 0 ? service : PreviewRecordService(),
          ),
        );
        await tester.pumpAndSettle();
        await tester.tap(find.text('기록'));
        await tester.pumpAndSettle();
        final controller = tester
            .widget<RecordsPanel>(find.byType(RecordsPanel))
            .controller;
        controller.select(file('voice.m4a'));
        controller.setConsent(true);
        final sending = controller.send();
        await tester.pump();
        final upload = controller.attempt!;
        await auth.signOut();
        await tester.pumpAndSettle();
        expect(upload.cancelToken.isCancelled, isTrue);
        expect(upload.onProgress, isNull);
        await auth.signIn('preview.b@example.com', PreviewAuthService.password);
        await tester.pumpAndSettle();
        // The old transport finishes after the new account is already signed in.
        transfer.finish.complete();
        await tester.pump();
        await sending;
        expect(transfer.cancelled, isTrue);
        expect(service.discarded, isEmpty);
        expect(controller.deletions, isEmpty);
        expect(controller.attempt, isNull);
        expect(auth.identity!.email, 'preview.b@example.com');
        await tester.tap(find.text('기록'));
        await tester.pumpAndSettle();
        expect(find.text('아직 올린 기록이 없어요.'), findsOneWidget);
        expect(tester.takeException(), isNull);
      },
    );

    test(
      'a pending cancellation cannot recreate deletion state after disposal',
      () async {
        final service = _TransferringRecords(_CancelOnTransfer())
          ..pendingDiscard = Completer<void>();
        final controller = RecordController(service);
        controller.select(file('voice.m4a'));
        controller.setConsent(true);
        final sending = controller.send();
        await pumpEventQueue();
        expect(service.discarded, hasLength(1));
        controller.dispose();
        service.pendingDiscard!.complete();
        await sending;
        expect(controller.deletions, isEmpty);
        expect(controller.attempt, isNull);
      },
    );
  });

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

    test(
      'timeout aborts a stalled file read before retrying the same upload',
      () async {
        var fileCancelled = false;
        var requests = 0;
        var activeRequests = 0;
        var reads = 0;
        final source = StreamController<List<int>>(
          onCancel: () {
            fileCancelled = true;
          },
        );
        final client = MockClient.streaming((request, stream) async {
          requests++;
          activeRequests++;
          if (requests == 1) {
            final subscription = stream.listen((_) {});
            await (request as http.Abortable).abortTrigger;
            await subscription.cancel();
            activeRequests--;
            throw http.RequestAbortedException(request.url);
          }
          expect(activeRequests, 1);
          await stream.drain<void>();
          activeRequests--;
          return http.StreamedResponse(const Stream.empty(), 200);
        });
        addTearDown(client.close);
        final transport = HttpRecordFileTransport(
          client: client,
          resolveTarget: (_) async => target,
          timeout: const Duration(milliseconds: 50),
        );
        final upload = _upload(
          RecordFile(
            name: 'voice.m4a',
            sizeBytes: 2,
            openRead: () => reads++ == 0 ? source.stream : Stream.value([1, 2]),
          ),
        );
        source.add([1]);
        await expectLater(
          transport.ensureUploaded(upload),
          throwsA(
            isA<BackendFailure>()
                .having((e) => e.kind, 'kind', BackendFailureKind.network)
                .having((e) => e.retryable, 'retryable', isTrue),
          ),
        );
        expect(fileCancelled, isTrue);
        expect(upload.cancelToken.isCancelled, isFalse);
        final previousProgress = upload.bytesSent;
        source.add([2]);
        await Future<void>.delayed(Duration.zero);
        expect(upload.bytesSent, previousProgress);
        expect(activeRequests, 0);
        await transport.ensureUploaded(upload);
        expect(requests, 2);
        expect(upload.progress, 1);
        await source.close();
      },
    );

    for (final fileError in [StateError('file unreadable'), 'grew', 'shrank']) {
      test(
        'file error $fileError aborts without waiting for a server response',
        () async {
          var aborted = false;
          final client = MockClient.streaming((request, stream) async {
            final subscription = stream.listen((_) {});
            await (request as http.Abortable).abortTrigger;
            aborted = true;
            await subscription.cancel();
            throw http.RequestAbortedException(request.url);
          });
          addTearDown(client.close);
          final transport = HttpRecordFileTransport(
            client: client,
            resolveTarget: (_) async => target,
            timeout: const Duration(minutes: 1),
          );
          final upload = _upload(
            RecordFile(
              name: 'voice.m4a',
              sizeBytes: 2,
              openRead: () => fileError is StateError
                  ? Stream.error(fileError)
                  : Stream.value(fileError == 'grew' ? [1, 2, 3] : [1]),
            ),
          );
          await expectLater(
            transport
                .ensureUploaded(upload)
                .timeout(const Duration(seconds: 2)),
            throwsA(
              fileError is StateError
                  ? same(fileError)
                  : isA<BackendFailure>().having(
                      (e) => e.code,
                      'code',
                      'FILE_CHANGED',
                    ),
            ),
          );
          expect(aborted, isTrue);
          expect(upload.cancelToken.isCancelled, isFalse);
        },
      );
    }

    test('timeout aborts a response body that never finishes', () async {
      var responseCancelled = false;
      final response = StreamController<List<int>>(
        onCancel: () {
          responseCancelled = true;
        },
      );
      final client = MockClient.streaming((request, stream) async {
        await stream.drain<void>();
        unawaited(
          (request as http.Abortable).abortTrigger!.then((_) {
            response.addError(http.RequestAbortedException(request.url));
            unawaited(response.close());
          }),
        );
        return http.StreamedResponse(response.stream, 200);
      });
      addTearDown(client.close);
      final transport = HttpRecordFileTransport(
        client: client,
        resolveTarget: (_) async => target,
        timeout: const Duration(milliseconds: 50),
      );
      await expectLater(
        transport.ensureUploaded(
          _upload(
            _chunks([
              [1, 2],
            ]),
          ),
        ),
        throwsA(
          isA<BackendFailure>().having(
            (e) => e.kind,
            'kind',
            BackendFailureKind.network,
          ),
        ),
      );
      await Future<void>.delayed(Duration.zero);
      expect(responseCancelled, isTrue);
    });

    for (final cancelByUser in [true, false]) {
      test(
        'stalled file is released on ${cancelByUser ? 'user cancellation' : 'network failure'}',
        () async {
          final reading = Completer<void>();
          var fileCancelled = false;
          final source = StreamController<List<int>>(
            onListen: reading.complete,
            onCancel: () {
              fileCancelled = true;
            },
          );
          final client = MockClient.streaming((request, stream) async {
            final subscription = stream.listen((_) {});
            await reading.future;
            if (cancelByUser) {
              await (request as http.Abortable).abortTrigger;
            }
            await subscription.cancel();
            throw cancelByUser
                ? http.RequestAbortedException(request.url)
                : http.ClientException('connection lost', request.url);
          });
          addTearDown(client.close);
          final transport = HttpRecordFileTransport(
            client: client,
            resolveTarget: (_) async => target,
            timeout: const Duration(minutes: 1),
          );
          final upload = _upload(
            RecordFile(
              name: 'voice.m4a',
              sizeBytes: 2,
              openRead: () => source.stream,
            ),
          );
          final result = expectLater(
            transport
                .ensureUploaded(upload)
                .timeout(const Duration(seconds: 2)),
            throwsA(
              cancelByUser
                  ? isA<UploadCancelled>()
                  : isA<BackendFailure>().having(
                      (e) => e.kind,
                      'kind',
                      BackendFailureKind.network,
                    ),
            ),
          );
          await reading.future;
          if (cancelByUser) upload.cancelToken.cancel();
          await result;
          expect(fileCancelled, isTrue);
          await source.close();
        },
      );
    }

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

    for (final stage in [UploadStage.reserve, UploadStage.complete]) {
      test(
        'disposal during $stage lookup sends no follow-up request',
        () async {
          final auth = await _auth();
          addTearDown(auth.dispose);
          final started = Completer<void>();
          final pending = Completer<http.Response>();
          final calls = <String>[];
          final client = MockClient((request) async {
            calls.add(request.method);
            started.complete();
            return pending.future;
          });
          addTearDown(client.close);
          final controller = RecordController(service(client, auth));
          controller.select(file('voice.m4a'));
          controller.setConsent(true);
          final upload = _upload(controller.selected!)
            ..stage = stage
            ..reservationRequested = true;
          controller.attempt = upload;
          final sending = controller.send();
          await started.future;
          controller.dispose();
          pending.complete(
            stage == UploadStage.reserve
                ? http.Response('{}', 404)
                : http.Response(jsonEncode(recordJson(upload.id)), 200),
          );
          await sending;
          expect(calls, ['GET']);
          expect(upload.stage, stage);
          expect(upload.cancelToken.isCancelled, isTrue);
          expect(controller.deletions, isEmpty);
          expect(controller.uploadError, isNull);
        },
      );
    }

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
      final job = await api.discardUpload(upload);
      expect(job!.id, 'job');
      expect(job.status, 'pending');
      expect(job.complete, isFalse);
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
      expect(await api.discardUpload(upload), isNull);
      missing = true;
      expect(await api.discardUpload(upload), isNull);
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

class _WaitingTransfer extends RecordFileTransport {
  final finish = Completer<void>();
  bool cancelled = false;

  @override
  Future<void> ensureUploaded(RecordUpload upload) async {
    await upload.cancelToken.whenCancelled;
    cancelled = true;
    await finish.future;
    throw const UploadCancelled();
  }
}
