import 'dart:async';
import 'dart:convert';

import 'package:fake_async/fake_async.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/records/api_record_service.dart';
import 'package:mobile/records/deletion_sheet.dart';
import 'package:mobile/records/personalization.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_deletion.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_review.dart';
import 'package:mobile/records/record_playback.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/records_panel.dart';
import 'package:mobile/services/backend_service.dart';

import 'records_test.dart' show ApiTestAuth, Transfer, file, recordJson;

DeletionJob _job(
  String status,
  List<String> remaining, {
  String type = 'record',
  int? attempts,
  String? error,
}) => DeletionJob(
  id: 'job',
  targetId: type == 'record' ? 'r' : 'account',
  targetType: type,
  status: status,
  remaining: remaining,
  attemptCount: attempts,
  lastErrorCode: error,
);

class _Pollable extends PreviewRecordService {
  int calls = 0;
  RecordStatus status = RecordStatus.processing;
  Object? failure;
  @override
  Future<List<RecordEntry>> list() async {
    calls++;
    if (failure != null) throw failure!;
    return [
      RecordEntry(
        id: 'r',
        name: 'voice.m4a',
        kind: RecordKind.audio,
        status: status,
        createdAt: DateTime.utc(2026, 11, 1),
      ),
    ];
  }
}

class _Checking extends PreviewRecordService {
  _Checking(this.replies);
  final List<DeletionJob> replies;
  int checks = 0;
  // The draft has no way to resume a failed deletion worker.
  @override
  bool get canRetryDeletion => false;
  @override
  Future<DeletionJob> deletionJob(String jobId) async {
    final index = checks < replies.length ? checks : replies.length - 1;
    checks++;
    return replies[index];
  }
}

class _Listed extends PreviewRecordService {
  _Listed(this.entries);
  final List<RecordEntry> entries;
  @override
  Future<List<RecordEntry>> list() async => entries;
}

RecordEntry _ready(
  String id,
  String name,
  ReviewProfileState state, {
  bool confirmed = true,
}) => RecordEntry(
  id: id,
  name: name,
  kind: RecordKind.text,
  status: RecordStatus.ready,
  createdAt: DateTime.utc(2026, 11, 1),
  textConfirmed: confirmed,
  personalization: state,
);

void main() {
  group('personalization state', () {
    for (final evidence in ProfileEvidence.values) {
      test('profile $evidence establishes availability only', () {
        final state = derivePersonalization(evidence);
        expect(
          state,
          evidence == ProfileEvidence.ready
              ? ReviewProfileState.available
              : ReviewProfileState.unknown,
        );
        expect(state, isNot(ReviewProfileState.reflected));
        expect(state, isNot(ReviewProfileState.processing));
        expect(state, isNot(ReviewProfileState.delayed));
      });
    }

    test('labels cover every state', () {
      final labels = {
        for (final s in ReviewProfileState.values)
          s: _ready(
            'x',
            'x',
            s,
            confirmed: s != ReviewProfileState.unknown,
          ).statusLabel,
      };
      expect(labels, {
        ReviewProfileState.unknown: '확인 필요',
        ReviewProfileState.available: '확정됨',
        ReviewProfileState.processing: '반영 중',
        ReviewProfileState.reflected: '반영됨',
        ReviewProfileState.delayed: '반영 지연',
        ReviewProfileState.failed: '반영 실패',
      });
      expect(
        _ready('x', 'x', ReviewProfileState.processing).inProgress,
        isTrue,
      );
      expect(
        _ready('x', 'x', ReviewProfileState.reflected).inProgress,
        isFalse,
      );
    });
  });

  group('ApiRecordService list', () {
    testWidgets(
      'refresh follows new drafts, confirmations and failed version lookups',
      (tester) async {
        final auth = ApiTestAuth();
        addTearDown(auth.dispose);
        await auth.signIn('preview.a@example.com', PreviewAuthService.password);
        final t1 = DateTime.utc(2026, 11, 1);
        final t2 = DateTime.utc(2026, 11, 2);
        Map<String, Object?> version(
          String recordId,
          int revision,
          String status,
          DateTime at,
        ) => {
          'id': '$recordId-$revision',
          'record_id': recordId,
          'revision': revision,
          'content': 'text-$revision',
          'status': status,
          'updated_at': at.toIso8601String(),
          'confirmed_at': status == 'confirmed' ? at.toIso8601String() : null,
        };
        var versions = [version('A', 1, 'confirmed', t1)];
        var failLookup = false;
        final client = MockClient((request) async {
          final path = request.url.path;
          if (path.endsWith('style-profile')) {
            return http.Response('null', 200);
          }
          if (path == '/records/A/text-versions') {
            return http.Response(
              failLookup ? '{}' : jsonEncode({'items': versions}),
              failLookup ? 503 : 200,
            );
          }
          if (path == '/records/B/text-versions') {
            return http.Response(
              jsonEncode({
                'items': [version('B', 1, 'confirmed', t1)],
              }),
              200,
            );
          }
          final records = [
            for (final id in ['A', 'B'])
              recordJson(id, status: 'ready')..['original_name'] = '$id.txt',
          ];
          if (path == '/records/A') {
            return http.Response(jsonEncode(records.first), 200);
          }
          return http.Response(
            jsonEncode({'items': path == '/records' ? records : []}),
            200,
          );
        });
        addTearDown(client.close);
        final service = ApiRecordService(
          client: client,
          baseUrl: Uri.parse('https://example.invalid'),
          auth: auth,
          personaId: 'persona',
          fileTransport: Transfer(),
          policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
        );
        final controller = RecordController(service);
        addTearDown(controller.dispose);
        RecordEntry a() => controller.entries.firstWhere((e) => e.id == 'A');
        await controller.refresh();
        expect(a().textConfirmed, isTrue);
        expect(a().confirmedAt, t1);
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(body: RecordsPanel(controller: controller)),
          ),
        );
        await tester.pumpAndSettle();

        // A newer draft can coexist with the previous confirmed version.
        versions = [
          version('A', 1, 'confirmed', t1),
          version('A', 2, 'draft', t2),
        ];
        await controller.refresh();
        await tester.pumpAndSettle();
        expect(a().statusLabel, '확인 필요');
        expect(a().confirmedAt, isNull);
        await tester.tap(find.text('A.txt'));
        await tester.pumpAndSettle();
        expect(find.text('전사 확인'), findsOneWidget);
        expect(find.text('text-2'), findsOneWidget);
        await tester.tap(find.byType(BackButton));
        await tester.pumpAndSettle();

        // Confirming revision 2 supersedes revision 1 and updates the date.
        versions = [
          version('A', 1, 'superseded', t1),
          version('A', 2, 'confirmed', t2),
        ];
        await controller.refresh();
        await tester.pumpAndSettle();
        expect(a().textConfirmed, isTrue);
        expect(a().confirmedAt, t2);
        await tester.tap(find.text('A.txt'));
        await tester.pumpAndSettle();
        expect(find.text('기록 상세'), findsOneWidget);
        expect(find.text('text-2'), findsOneWidget);
        expect(find.text('확정일    2026.11.02'), findsOneWidget);
        await tester.tap(find.byType(BackButton));
        await tester.pumpAndSettle();

        failLookup = true;
        await controller.refresh();
        expect(a().textConfirmed, isFalse);
        expect(a().confirmedAt, isNull);
        expect(a().personalization, ReviewProfileState.unknown);
        expect(
          controller.entries.firstWhere((e) => e.id == 'B').textConfirmed,
          isTrue,
        );
        expect(controller.listError, isNull);
        failLookup = false;
        versions = [];
        await controller.refresh();
        expect(a().textConfirmed, isFalse);
        expect(a().confirmedAt, isNull);
      },
    );

    test(
      'overlapping lists keep version evidence local to each response',
      () async {
        final auth = ApiTestAuth();
        addTearDown(auth.dispose);
        await auth.signIn('preview.a@example.com', PreviewAuthService.password);
        final started = Completer<void>();
        final pending = Completer<http.Response>();
        var lookups = 0;
        Map<String, Object?> version(int revision, String status) => {
          'id': 'v$revision',
          'record_id': 'A',
          'revision': revision,
          'content': 'text',
          'status': status,
          'updated_at': '2026-11-01T00:00:00Z',
          'confirmed_at': status == 'confirmed' ? '2026-11-01T00:00:00Z' : null,
        };
        final client = MockClient((request) async {
          if (request.url.path.endsWith('text-versions')) {
            lookups++;
            if (lookups == 1) {
              started.complete();
              return pending.future;
            }
            return http.Response(
              jsonEncode({
                'items': [version(2, 'draft')],
              }),
              200,
            );
          }
          if (request.url.path.endsWith('style-profile')) {
            return http.Response('null', 200);
          }
          return http.Response(
            jsonEncode({
              'items': [recordJson('A', status: 'ready')],
            }),
            200,
          );
        });
        addTearDown(client.close);
        final service = ApiRecordService(
          client: client,
          baseUrl: Uri.parse('https://example.invalid'),
          auth: auth,
          personaId: 'persona',
          fileTransport: Transfer(),
          policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
        );
        final older = service.list();
        await started.future;
        expect((await service.list()).single.textConfirmed, isFalse);
        pending.complete(
          http.Response(
            jsonEncode({
              'items': [version(1, 'confirmed')],
            }),
            200,
          ),
        );
        expect((await older).single.textConfirmed, isTrue);
        expect((await service.list()).single.textConfirmed, isFalse);
        expect(lookups, 3);
      },
    );

    for (final response in ['ready', 'null', 'error']) {
      testWidgets(
        'confirmed record stays unproven with profile $response and does not poll',
        (tester) async {
          final auth = ApiTestAuth();
          addTearDown(auth.dispose);
          await auth.signIn(
            'preview.a@example.com',
            PreviewAuthService.password,
          );
          final t = DateTime.utc(2026, 11, 1);
          var requests = 0;
          final client = MockClient((request) async {
            requests++;
            final path = request.url.path;
            if (path.endsWith('style-profile')) {
              return response == 'error'
                  ? http.Response('{}', 503)
                  : http.Response(
                      response == 'null'
                          ? 'null'
                          : jsonEncode({
                              'status': 'ready',
                              'persona_id': 'persona',
                              'updated_at': t
                                  .add(const Duration(days: 100))
                                  .toIso8601String(),
                            }),
                      200,
                    );
            }
            if (path.endsWith('text-versions')) {
              return http.Response(
                jsonEncode({
                  'items': [
                    {
                      'id': 'vA',
                      'record_id': 'A',
                      'revision': 1,
                      'content': '확정한 합성 전사문',
                      'status': 'confirmed',
                      'updated_at': t.toIso8601String(),
                      'confirmed_at': t.toIso8601String(),
                    },
                  ],
                }),
                200,
                headers: {'content-type': 'application/json; charset=utf-8'},
              );
            }
            final record = recordJson('A', status: 'ready');
            if (path == '/records/A') {
              return http.Response(jsonEncode(record), 200);
            }
            return http.Response(
              jsonEncode({
                'items': path == '/records' ? [record] : [],
              }),
              200,
            );
          });
          addTearDown(client.close);
          final service = ApiRecordService(
            client: client,
            baseUrl: Uri.parse('https://example.invalid'),
            auth: auth,
            personaId: 'persona',
            fileTransport: Transfer(),
            policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
          );
          expect(await service.list(), hasLength(1));
          final controller = RecordController(service);
          addTearDown(controller.dispose);
          await controller.refresh();
          expect(controller.entries.single.statusLabel, '확정됨');
          expect(controller.hasPending, isFalse);
          await tester.pumpWidget(
            MaterialApp(
              home: Scaffold(body: RecordsPanel(controller: controller)),
            ),
          );
          await tester.pumpAndSettle();
          await tester.tap(find.text('synthetic.txt'));
          await tester.pumpAndSettle();
          expect(find.text('기록 상세'), findsOneWidget);
          expect(find.text('확정한 합성 전사문'), findsOneWidget);
          expect(
            find.textContaining('프로필·검색 색인 반영 상태는 확인이 필요해요.'),
            findsOneWidget,
          );
          expect(
            find.byKey(const ValueKey('personalization-reflected')),
            findsNothing,
          );
          expect(
            find.byKey(const ValueKey('personalization-processing')),
            findsNothing,
          );
          expect(
            find.byKey(const ValueKey('personalization-delayed')),
            findsNothing,
          );
          final before = requests;
          await tester.pump(const Duration(minutes: 20));
          expect(
            requests,
            before,
            reason: 'availability does not imply processing',
          );
          expect(
            find.textContaining('프로필·검색 색인 반영 상태는 확인이 필요해요.'),
            findsOneWidget,
          );
        },
      );
    }

    test(
      'marks confirmed records without inferring reflection from timestamps',
      () async {
        final auth = ApiTestAuth();
        addTearDown(auth.dispose);
        await auth.signIn('preview.a@example.com', PreviewAuthService.password);
        final t = DateTime.utc(2026, 11, 1, 12);
        Object? profile; // null body, or a profile map
        final hits = <String, int>{};
        final client = MockClient((request) async {
          final path = request.url.path;
          hits.update(path, (n) => n + 1, ifAbsent: () => 1);
          if (path == '/records') {
            return http.Response(
              jsonEncode({
                'items': [
                  recordJson('A', status: 'ready'),
                  recordJson('B', status: 'ready'),
                  recordJson('C', status: 'processing'),
                ],
              }),
              200,
            );
          }
          if (path == '/records/A/text-versions') {
            return http.Response(
              jsonEncode({
                'items': [
                  {
                    'id': 'vA',
                    'record_id': 'A',
                    'revision': 1,
                    'content': 'x',
                    'status': 'confirmed',
                    'updated_at': t.toIso8601String(),
                    'confirmed_at': t.toIso8601String(),
                  },
                ],
              }),
              200,
            );
          }
          if (path == '/records/B/text-versions') {
            return http.Response(
              jsonEncode({
                'items': [
                  {
                    'id': 'vB',
                    'record_id': 'B',
                    'revision': 1,
                    'content': 'x',
                    'status': 'draft',
                    'updated_at': t.toIso8601String(),
                  },
                ],
              }),
              200,
            );
          }
          if (path == '/personas/persona/style-profile') {
            return http.Response(jsonEncode(profile), 200);
          }
          return http.Response('{}', 404);
        });
        addTearDown(client.close);
        final service = ApiRecordService(
          client: client,
          baseUrl: Uri.parse('https://example.invalid'),
          auth: auth,
          personaId: 'persona',
          policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
          fileTransport: Transfer(),
        );
        Map<String, String> labels(List<RecordEntry> list) => {
          for (final e in list) e.id: e.statusLabel,
        };

        expect(labels(await service.list()), {
          'A': '확정됨',
          'B': '확인 필요',
          'C': '처리 중',
        });
        expect(hits['/records/A/text-versions'], 1);
        expect(hits['/records/B/text-versions'], 1);

        profile = {
          'id': 'p',
          'persona_id': 'persona',
          'status': 'ready',
          'updated_at': t.add(const Duration(minutes: 1)).toIso8601String(),
        };
        for (final profileAt in [
          t.subtract(const Duration(days: 1)),
          t,
          t.add(const Duration(minutes: 1)),
          t.add(const Duration(days: 100)),
        ]) {
          (profile as Map<String, Object?>)['updated_at'] = profileAt
              .toIso8601String();
          final list = await service.list();
          expect(labels(list)['A'], '확정됨');
          final confirmed = list.firstWhere((e) => e.id == 'A');
          expect(confirmed.textConfirmed, isTrue);
          expect(confirmed.confirmedAt, t);
          expect(confirmed.personalization, ReviewProfileState.available);
          expect(confirmed.inProgress, isFalse);
        }
        // Even consecutive list requests recheck confirmed and draft versions.
        expect(hits['/records/A/text-versions'], 5);
        expect(hits['/records/B/text-versions'], 5);
        expect(hits['/records/C/text-versions'], isNull);
        await service.list();
        expect(hits['/records/B/text-versions'], 6);
        expect(hits['/records/A/text-versions'], 6);

        profile = null;
        final later = (await service.list()).firstWhere((e) => e.id == 'A');
        expect(later.statusLabel, '확정됨');
        expect(later.personalization, ReviewProfileState.unknown);
        expect(later.inProgress, isFalse);
      },
    );

    test(
      'a failed lookup leaves that record unconfirmed instead of failing',
      () async {
        final auth = ApiTestAuth();
        addTearDown(auth.dispose);
        await auth.signIn('preview.a@example.com', PreviewAuthService.password);
        final client = MockClient((request) async {
          if (request.url.path == '/records') {
            return http.Response(
              jsonEncode({
                'items': [recordJson('A', status: 'ready')],
              }),
              200,
            );
          }
          return http.Response('{}', 500);
        });
        addTearDown(client.close);
        final service = ApiRecordService(
          client: client,
          baseUrl: Uri.parse('https://example.invalid'),
          auth: auth,
          personaId: 'persona',
          policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
          fileTransport: Transfer(),
        );
        expect((await service.list()).single.statusLabel, '확인 필요');
      },
    );
  });

  group('optional hooks the draft does not define', () {
    ApiRecordService build(
      http.Client client,
      ApiTestAuth auth, {
      RecordApiExtensions extensions = const RecordApiExtensions(),
    }) => ApiRecordService(
      client: client,
      baseUrl: Uri.parse('https://example.invalid'),
      auth: auth,
      personaId: 'persona',
      policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
      fileTransport: Transfer(),
      extensions: extensions,
    );

    test('everything stays disabled until the request is agreed', () async {
      final auth = ApiTestAuth();
      addTearDown(auth.dispose);
      final service = build(
        MockClient((_) async => http.Response('{}', 500)),
        auth,
      );
      expect(service.canRetryProcessing, isFalse);
      expect(service.canRetryDeletion, isFalse);
      expect(service.canDeleteAccount, isFalse);
      expect(service.canPlayOriginal, isFalse);
      expect(() => service.retryProcessing('r'), throwsUnsupportedError);
      expect(() => service.deleteAccount(), throwsUnsupportedError);
      expect(() => service.originalAudio('r'), throwsUnsupportedError);
      expect(
        () => service.retryDeletion(_job('failed', ['storage'])),
        throwsUnsupportedError,
      );
    });

    test(
      'an agreed retry request is sent authenticated and refreshes',
      () async {
        final auth = ApiTestAuth();
        addTearDown(auth.dispose);
        await auth.signIn('preview.a@example.com', PreviewAuthService.password);
        final seen = <String>[];
        final client = MockClient((request) async {
          expect(
            request.headers['Authorization'],
            'Bearer synthetic-test-token',
          );
          seen.add('${request.method} ${request.url.path}');
          if (request.url.path == '/records') {
            return http.Response(jsonEncode({'items': <Object>[]}), 200);
          }
          if (request.url.path == '/deletion-jobs') {
            return http.Response(jsonEncode({'items': <Object>[]}), 200);
          }
          return http.Response('{}', 202);
        });
        addTearDown(client.close);
        final service = build(
          client,
          auth,
          extensions: RecordApiExtensions(
            retryProcessing: (api, id) async {
              await api.send('POST', '/agreed/$id/retry');
            },
          ),
        );
        expect(service.canRetryProcessing, isTrue);
        final controller = RecordController(service);
        addTearDown(controller.dispose);
        await controller.retry(
          RecordEntry(
            id: 'r1',
            name: 'a.m4a',
            kind: RecordKind.audio,
            status: RecordStatus.failed,
            createdAt: DateTime.utc(2026, 11, 1),
          ),
        );
        expect(seen.first, 'POST /agreed/r1/retry');
        expect(seen, contains('GET /records'));
        expect(controller.actionError, isNull);
      },
    );

    test(
      'agreed audio, deletion retry and account deletion are exposed',
      () async {
        final auth = ApiTestAuth();
        addTearDown(auth.dispose);
        await auth.signIn('preview.a@example.com', PreviewAuthService.password);
        final client = MockClient((_) async => http.Response('{}', 200));
        addTearDown(client.close);
        final service = build(
          client,
          auth,
          extensions: RecordApiExtensions(
            originalAudio: (api, id) async =>
                RecordAudioSource(url: Uri.parse('https://audio.invalid/$id')),
            retryDeletion: (api, job) async => _job('running', job.remaining),
            deleteAccount: (api) async =>
                _job('pending', ['storage', 'auth'], type: 'account'),
          ),
        );
        expect(service.canPlayOriginal, isTrue);
        expect(service.canRetryDeletion, isTrue);
        expect(service.canDeleteAccount, isTrue);
        expect(
          (await service.originalAudio('r1')).url.toString(),
          'https://audio.invalid/r1',
        );
        expect((await service.deleteAccount()).targetType, 'account');
        expect(
          (await service.retryDeletion(_job('failed', ['storage']))).status,
          'running',
        );
      },
    );
  });

  group('status polling', () {
    test('checks quietly while processing and stops when done', () {
      fakeAsync((async) {
        final service = _Pollable();
        final controller = RecordController(service);
        controller.startPolling();
        controller.refresh();
        async.flushMicrotasks();
        expect(service.calls, 1);
        expect(controller.hasPending, isTrue);
        expect(controller.loading, isFalse);
        async.elapse(const Duration(seconds: 5));
        async.flushMicrotasks();
        expect(service.calls, 2);
        service.status = RecordStatus.ready;
        async.elapse(const Duration(seconds: 5));
        async.flushMicrotasks();
        expect(service.calls, 3);
        expect(controller.entries.single.status, RecordStatus.ready);
        expect(controller.hasPending, isFalse);
        async.elapse(const Duration(minutes: 5));
        expect(service.calls, 3, reason: 'nothing left to wait for');
        controller.dispose();
      });
    });

    test('backs off after failures and recovers', () {
      fakeAsync((async) {
        final service = _Pollable();
        final controller = RecordController(service);
        controller.startPolling();
        controller.refresh();
        async.flushMicrotasks();
        service.failure = const BackendFailure(
          BackendFailureKind.network,
          retryable: true,
        );
        async.elapse(const Duration(seconds: 5));
        async.flushMicrotasks();
        expect(service.calls, 2);
        async.elapse(const Duration(seconds: 5));
        expect(service.calls, 2, reason: 'next check is 10 s after a failure');
        async.elapse(const Duration(seconds: 5));
        async.flushMicrotasks();
        expect(service.calls, 3);
        service.failure = null;
        async.elapse(const Duration(seconds: 20));
        async.flushMicrotasks();
        expect(service.calls, 4);
        async.elapse(const Duration(seconds: 5));
        async.flushMicrotasks();
        expect(service.calls, 5, reason: 'back to the base interval');
        controller.dispose();
      });
    });

    test('a dead session stops the checks', () {
      fakeAsync((async) {
        final service = _Pollable();
        final controller = RecordController(service);
        controller.startPolling();
        controller.refresh();
        async.flushMicrotasks();
        service.failure = const BackendFailure.unauthenticated();
        async.elapse(const Duration(seconds: 5));
        async.flushMicrotasks();
        async.elapse(const Duration(minutes: 5));
        expect(service.calls, 2);
        controller.dispose();
      });
    });

    test('nothing is scheduled until polling is switched on', () {
      fakeAsync((async) {
        final service = _Pollable();
        final controller = RecordController(service);
        controller.refresh();
        async.flushMicrotasks();
        async.elapse(const Duration(minutes: 1));
        expect(service.calls, 1);
        controller.startPolling();
        async.elapse(const Duration(seconds: 5));
        async.flushMicrotasks();
        expect(service.calls, 2);
        controller.stopPolling();
        async.elapse(const Duration(minutes: 1));
        expect(service.calls, 2);
        controller.dispose();
      });
    });

    testWidgets('the list screen polls only while it is on screen', (
      tester,
    ) async {
      final service = _Pollable();
      final controller = RecordController(
        service,
        pollInterval: const Duration(seconds: 1),
      );
      addTearDown(controller.dispose);
      await controller.refresh();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: RecordsPanel(controller: controller)),
        ),
      );
      await tester.pump(const Duration(milliseconds: 1100));
      expect(service.calls, 2);
      await tester.pumpWidget(const MaterialApp(home: SizedBox()));
      final before = service.calls;
      await tester.pump(const Duration(seconds: 10));
      expect(service.calls, before);
    });
  });

  group('SC-03 badges', () {
    testWidgets('reflection states are shown on the list', (tester) async {
      final controller = RecordController(
        _Listed([
          _ready('a', '반영중기록.txt', ReviewProfileState.processing),
          _ready('b', '지연기록.txt', ReviewProfileState.delayed),
          _ready('c', '실패기록.txt', ReviewProfileState.failed),
          _ready('d', '반영기록.txt', ReviewProfileState.reflected),
          _ready('e', '확정기록.txt', ReviewProfileState.unknown),
          _ready(
            'f',
            '미확정기록.txt',
            ReviewProfileState.unknown,
            confirmed: false,
          ),
        ]),
      );
      addTearDown(controller.dispose);
      await controller.refresh();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: RecordsPanel(controller: controller)),
        ),
      );
      await tester.pump(const Duration(milliseconds: 50));
      for (final label in ['반영 중', '반영 지연', '반영 실패', '반영됨', '확정됨', '확인 필요']) {
        expect(find.text(label), findsOneWidget, reason: label);
      }
      controller.stopPolling();
    });
  });

  group('deletion progress', () {
    test('parses attempts and error codes; steps follow the target', () {
      final job = DeletionJob.fromJson({
        'id': 'j',
        'target_id': 'r',
        'target_type': 'record',
        'status': 'failed',
        'remaining_steps': ['external_index'],
        'attempt_count': 3,
        'last_error_code': 'index_timeout',
      });
      expect(job.attemptCount, 3);
      expect(job.lastErrorCode, 'index_timeout');
      expect(job.failed, isTrue);
      expect(job.running, isFalse);
      expect(job.steps, ['storage', 'database', 'external_index', 'adapter']);
      expect(job.doneSteps, ['storage', 'database', 'adapter']);
      expect(_job('pending', ['auth'], type: 'account').steps.last, 'auth');
      expect(
        _job('completed', ['database']).failed,
        isTrue,
        reason: 'completed with leftovers is a partial failure',
      );
      expect(_job('running', ['storage']).running, isTrue);
      expect(_job('completed', []).complete, isTrue);
      expect(
        _job('running', ['storage', 'auth'], type: 'account').remainingLabel,
        '원본 파일 · 계정 정보',
      );
    });

    test('a running job is re-checked on its own, a failed one is not', () {
      fakeAsync((async) {
        final service = _Checking([
          _job('running', ['external_index']),
          _job('completed', []),
        ]);
        final controller = RecordDeletionController(
          service: service,
          targetId: 'r',
          job: _job('pending', ['storage', 'database', 'external_index']),
        );
        controller.watch(interval: const Duration(seconds: 3));
        async.elapse(const Duration(seconds: 3));
        async.flushMicrotasks();
        expect(service.checks, 1);
        expect(controller.job!.remaining, ['external_index']);
        async.elapse(const Duration(seconds: 3));
        async.flushMicrotasks();
        expect(controller.complete, isTrue);
        async.elapse(const Duration(minutes: 1));
        expect(service.checks, 2);
        controller.dispose();

        final failedService = _Checking([_job('completed', [])]);
        final failed = RecordDeletionController(
          service: failedService,
          targetId: 'r',
          job: _job('failed', ['storage'], attempts: 2),
        );
        failed.watch(interval: const Duration(seconds: 3));
        async.elapse(const Duration(minutes: 1));
        expect(failedService.checks, 0);
        failed.dispose();
      });
    });

    test('watching is bounded', () {
      fakeAsync((async) {
        final service = _Checking([
          _job('running', ['storage']),
        ]);
        final controller = RecordDeletionController(
          service: service,
          targetId: 'r',
          job: _job('running', ['storage']),
        );
        controller.watch(interval: const Duration(seconds: 1), maxChecks: 3);
        async.elapse(const Duration(minutes: 1));
        async.flushMicrotasks();
        expect(service.checks, 3);
        controller.dispose();
      });
    });

    testWidgets(
      'the sheet lists finished and remaining steps and follows the job',
      (tester) async {
        final service = _Checking([
          _job('running', ['external_index', 'adapter']),
          _job('completed', []),
        ]);
        final controller = RecordDeletionController(
          service: service,
          targetId: 'r',
          job: _job('pending', [
            'storage',
            'database',
            'external_index',
            'adapter',
          ]),
        );
        addTearDown(controller.dispose);
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: DeletionSheet(
                controller: controller,
                autoCheck: const Duration(seconds: 1),
              ),
            ),
          ),
        );
        expect(find.text('삭제가 진행 중이에요. 아직 완료되지 않았어요.'), findsOneWidget);
        expect(find.byKey(const ValueKey('deletion-steps')), findsOneWidget);
        expect(find.text('진행 중'), findsNWidgets(4));
        await tester.pump(const Duration(milliseconds: 1100));
        await tester.pump(const Duration(milliseconds: 100));
        expect(find.text('남은 항목: 검색 색인 · 개인화 산출물'), findsOneWidget);
        expect(find.text('삭제됨'), findsNWidgets(2));
        await tester.pump(const Duration(milliseconds: 1100));
        await tester.pump(const Duration(milliseconds: 100));
        expect(find.text('삭제를 완료했어요.'), findsOneWidget);
        expect(find.text('삭제됨'), findsNWidgets(4));
        expect(find.text('완료'), findsOneWidget);
        expect(service.checks, 2);
      },
    );

    testWidgets('a partial failure keeps what is left and says why', (
      tester,
    ) async {
      final service = _Checking([_job('completed', [])]);
      final controller = RecordDeletionController(
        service: service,
        targetId: 'r',
        job: _job(
          'failed',
          ['external_index'],
          attempts: 3,
          error: 'index_timeout',
        ),
      );
      addTearDown(controller.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: DeletionSheet(
              controller: controller,
              autoCheck: const Duration(seconds: 1),
            ),
          ),
        ),
      );
      await tester.pump(const Duration(seconds: 5));
      expect(service.checks, 0, reason: 'a failed job waits for the user');
      expect(find.text('일부 자료를 삭제하지 못했어요.'), findsOneWidget);
      expect(find.text('남은 항목: 검색 색인'), findsOneWidget);
      expect(find.text('시도 3회 · 오류 코드 index_timeout'), findsOneWidget);
      expect(find.text('남음'), findsOneWidget);
      expect(find.text('삭제됨'), findsNWidgets(3));
      expect(find.textContaining('다시 진행하는 방법은 아직 연결되지 않았어요'), findsOneWidget);
      await tester.tap(find.text('상태 다시 확인'));
      await tester.pumpAndSettle();
      expect(find.text('삭제를 완료했어요.'), findsOneWidget);
    });
  });

  group('account deletion cleanup', () {
    test('only the session that asked can end the account session', () async {
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await auth.signIn('preview.a@example.com', PreviewAuthService.password);
      final generation = auth.generation;
      await auth.completeAccountDeletion(requestGeneration: generation - 1);
      expect(auth.identity, isNotNull, reason: 'a stale request is ignored');
      await auth.completeAccountDeletion(requestGeneration: generation);
      expect(auth.identity, isNull);
      expect(auth.notice, contains('계정 삭제를 완료했어요'));
      // A second report for the same session changes nothing.
      auth.notice = null;
      await auth.completeAccountDeletion(requestGeneration: generation);
      expect(auth.notice, isNull);
    });

    test('finishing the account deletion drops every local copy', () async {
      final service = PreviewRecordService();
      final controller = RecordController(service);
      addTearDown(controller.dispose);
      controller.select(file('voice.m4a', 700));
      controller.setConsent(true);
      await controller.send();
      expect(controller.entries, isNotEmpty);
      controller.reviewDrafts['x'] = const RecordReviewDraft(content: '임시 수정');
      controller.select(file('next.m4a'));
      final deletion = controller.deletionFor('#account', account: true);
      await deletion.execute();
      expect(controller.entries, isNotEmpty, reason: 'pending is not done');
      await deletion.execute();
      expect(deletion.complete, isTrue);
      expect(controller.entries, isEmpty);
      expect(controller.reviewDrafts, isEmpty);
      expect(controller.selected, isNull);
      expect(controller.consent, isFalse);
      expect(controller.attempt, isNull);
    });
  });
}
