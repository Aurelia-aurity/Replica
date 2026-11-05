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
    final confirmed = DateTime.utc(2026, 11, 1, 12);
    ReviewProfileState derive(ProfileEvidence profile, Duration after) =>
        derivePersonalization(
          confirmedAt: confirmed,
          profile: profile,
          now: confirmed.add(after),
        );

    test('a profile published after confirming counts as reflected', () {
      expect(
        derive(
          ProfileEvidence.ready(confirmed.add(const Duration(seconds: 30))),
          const Duration(minutes: 1),
        ),
        ReviewProfileState.reflected,
      );
      expect(
        derive(ProfileEvidence.ready(confirmed), Duration.zero),
        ReviewProfileState.reflected,
      );
    });

    test(
      'no newer profile means reflecting, then delayed — never reflected',
      () {
        expect(
          derive(const ProfileEvidence.none(), const Duration(minutes: 2)),
          ReviewProfileState.processing,
        );
        expect(
          derive(
            ProfileEvidence.ready(confirmed.subtract(const Duration(days: 2))),
            const Duration(minutes: 2),
          ),
          ReviewProfileState.processing,
        );
        expect(
          derive(const ProfileEvidence.none(), personalizationWindow),
          ReviewProfileState.processing,
        );
        expect(
          derive(
            const ProfileEvidence.none(),
            personalizationWindow + const Duration(seconds: 1),
          ),
          ReviewProfileState.delayed,
        );
      },
    );

    test('an unchecked profile proves nothing', () {
      expect(
        derive(const ProfileEvidence.unavailable(), const Duration(hours: 5)),
        ReviewProfileState.unknown,
      );
    });

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
    test(
      'marks confirmed records and derives reflection, caching lookups',
      () async {
        final auth = ApiTestAuth();
        addTearDown(auth.dispose);
        await auth.signIn('preview.a@example.com', PreviewAuthService.password);
        final t = DateTime.utc(2026, 11, 1, 12);
        var now = t.add(const Duration(minutes: 2));
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
          clock: () => now,
        );
        Map<String, String> labels(List<RecordEntry> list) => {
          for (final e in list) e.id: e.statusLabel,
        };

        expect(labels(await service.list()), {
          'A': '반영 중',
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
        now = t.add(const Duration(minutes: 2, seconds: 10));
        expect(labels(await service.list())['A'], '반영됨');
        // Confirmed records and a recently seen draft are not looked up again.
        expect(hits['/records/A/text-versions'], 1);
        expect(hits['/records/B/text-versions'], 1);

        now = t.add(const Duration(minutes: 3));
        await service.list();
        expect(hits['/records/B/text-versions'], 2);
        expect(hits['/records/A/text-versions'], 1);

        profile = null;
        now = t.add(const Duration(hours: 1));
        expect(labels(await service.list())['A'], '반영 지연');
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
      controller.reviewDrafts['x'] = '임시 수정';
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
