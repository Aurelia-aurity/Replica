import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/main.dart';
import 'package:mobile/records/api_record_service.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_deletion.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/records_panel.dart';
import 'package:mobile/records/deletion_sheet.dart';

import 'records_test.dart' show ApiTestAuth, Transfer;
import 'record_review_test.dart' show ReviewFixture;

class DeletionFixture extends ReviewFixture {
  int requests = 0, checks = 0, retries = 0;
  bool fail = true, supported = true;
  Completer<void>? delayed;
  DeletionJob? current;
  @override
  bool get canDeleteAccount => supported;
  DeletionJob make(String status, List<String> remaining) => DeletionJob(
    id: 'job',
    targetId: current?.targetId ?? 'record',
    targetType: current?.targetType ?? 'record',
    status: status,
    remaining: remaining,
  );
  @override
  Future<List<RecordEntry>> list() async => current == null ? [entry] : [];
  @override
  Future<List<DeletionJob>> deletionJobs() async =>
      current == null ? [] : [current!];
  @override
  Future<DeletionJob> deleteRecord(String recordId) async {
    requests++;
    if (delayed != null) await delayed!.future;
    return current ??= make('pending', [
      'storage',
      'database',
      'external_index',
    ]);
  }

  @override
  Future<DeletionJob> deleteAccount() async {
    requests++;
    return current ??= const DeletionJob(
      id: 'job',
      targetId: 'account',
      targetType: 'account',
      status: 'pending',
      remaining: ['auth', 'storage'],
    );
  }

  @override
  Future<DeletionJob> deletionJob(String jobId) async {
    checks++;
    return current = fail
        ? make('failed', ['external_index'])
        : make('completed', []);
  }

  @override
  Future<DeletionJob> retryDeletion(DeletionJob job) async {
    retries++;
    return current = make('completed', []);
  }
}

void main() {
  test(
    'response for a different target cannot complete this deletion',
    () async {
      final service = DeletionFixture()
        ..current = const DeletionJob(
          id: 'job',
          targetId: 'another-record',
          targetType: 'record',
          status: 'completed',
          remaining: [],
        );
      final c = RecordDeletionController(service: service, targetId: 'record');
      addTearDown(c.dispose);
      expect(await c.execute(), isFalse);
      expect(c.complete, isFalse);
      expect(c.job, isNull);
      expect(c.error, isNotNull);
    },
  );
  test('only completed with empty remaining steps is complete', () {
    for (final status in ['pending', 'running', 'failed', 'completed']) {
      for (final remaining in [
        <String>[],
        ['storage'],
      ]) {
        expect(
          DeletionJob(
            id: 'j',
            targetId: 'r',
            targetType: 'record',
            status: status,
            remaining: remaining,
          ).complete,
          status == 'completed' && remaining.isEmpty,
        );
      }
    }
  });
  test(
    'request, partial failure and retry keep same job; duplicates blocked',
    () async {
      final service = DeletionFixture()..delayed = Completer<void>();
      final c = RecordDeletionController(service: service, targetId: 'record');
      addTearDown(c.dispose);
      final first = c.execute();
      expect(await c.execute(), isFalse);
      expect(service.requests, 1);
      service.delayed!.complete();
      expect(await first, isFalse);
      expect(c.job!.id, 'job');
      expect(await c.execute(), isFalse);
      expect(c.job!.remainingLabel, '검색 색인');
      expect(await c.execute(retry: true), isTrue);
      expect(service.retries, 1);
      expect(service.requests, 1);
    },
  );
  test('disposal suppresses delayed completion', () async {
    final service = DeletionFixture()..delayed = Completer<void>();
    final c = RecordDeletionController(service: service, targetId: 'record');
    final result = c.execute();
    c.dispose();
    service.delayed!.complete();
    expect(await result, isFalse);
    expect(c.job, isNull);
  });
  test('unavailable account contract cannot issue a request', () async {
    final service = DeletionFixture()..supported = false;
    final c = RecordDeletionController(
      service: service,
      targetId: '#account',
      account: true,
    );
    addTearDown(c.dispose);
    expect(await c.execute(), isFalse);
    expect(service.requests, 0);
  });
  test(
    'pending jobs are recoverable and completed record drafts are cleared',
    () async {
      final service = DeletionFixture();
      final records = RecordController(service);
      addTearDown(records.dispose);
      records.reviewDrafts['record'] = '합성 초안';
      await records.refresh();
      final c = records.deletionFor('record');
      await c.execute();
      expect(records.entries, isEmpty);
      await c.execute();
      expect(c.job!.status, 'failed');
      expect(records.reviewDrafts, isNotEmpty);
      await c.execute(retry: true);
      expect(records.reviewDrafts, isEmpty);
      final restored = RecordController(service);
      addTearDown(restored.dispose);
      await restored.refresh();
      expect(restored.deletions['record']!.complete, isTrue);
    },
  );
  testWidgets(
    'cancel sends nothing; pending job survives closing and partial failure can retry',
    (tester) async {
      final service = DeletionFixture()..confirmed = true;
      final records = RecordController(service);
      addTearDown(records.dispose);
      await records.refresh();
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: RecordsPanel(controller: records)),
        ),
      );
      await tester.tap(find.text(service.entry.name));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('기록 삭제'));
      await tester.pumpAndSettle();
      expect(find.text('이 기록을 삭제할까요?'), findsOneWidget);
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(service.requests, 0);
      await tester.tap(find.byTooltip('기록 삭제'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('delete-confirm')));
      await tester.pumpAndSettle();
      expect(find.text('삭제가 진행 중이에요. 아직 완료되지 않았어요.'), findsOneWidget);
      await tester.tap(find.text('닫기'));
      await tester.pumpAndSettle();
      expect(find.text(service.entry.name), findsNothing);
      await tester.tap(find.text('${service.entry.name} 삭제 요청'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('상태 다시 확인'));
      await tester.pumpAndSettle();
      expect(find.text('남은 항목: 검색 색인'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('delete-confirm')));
      await tester.pumpAndSettle();
      expect(find.text('미리보기 자료 삭제를 완료했어요.'), findsOneWidget);
      await tester.tap(find.text('완료'));
      await tester.pumpAndSettle();
      expect(service.requests, 1);
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets(
    'account deletion keeps login until verified completion then clears navigation',
    (tester) async {
      final auth = PreviewAuthService();
      addTearDown(auth.dispose);
      await auth.signIn('preview.a@example.com', PreviewAuthService.password);
      final service = DeletionFixture()..fail = false;
      await tester.pumpWidget(
        ReplicaApp(authService: auth, recordServiceFactory: () => service),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('계정'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('계정 삭제'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('delete-confirm')));
      await tester.pumpAndSettle();
      expect(auth.identity, isNotNull);
      await tester.tap(find.text('상태 다시 확인'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('완료'));
      await tester.pumpAndSettle();
      expect(auth.identity, isNull);
      expect(find.text('계정과 모든 자료를 삭제할까요?'), findsNothing);
      expect(find.byKey(const ValueKey('auth-email')), findsOneWidget);
    },
  );
  testWidgets(
    'unsupported account deletion sheet fits small screen and cannot submit',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(320, 568);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      final service = DeletionFixture()..supported = false;
      final c = RecordDeletionController(
        service: service,
        targetId: '#account',
        account: true,
      );
      addTearDown(c.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: DeletionSheet(controller: c)),
        ),
      );
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.byKey(const ValueKey('delete-confirm')),
        150,
      );
      expect(
        tester
            .widget<FilledButton>(find.byKey(const ValueKey('delete-confirm')))
            .onPressed,
        isNull,
      );
      expect(tester.takeException(), isNull);
    },
  );
  test('API deletion request is not success and never invents account or retry endpoints', () async {
    final auth = ApiTestAuth();
    addTearDown(auth.dispose);
    await auth.signIn('preview.a@example.com', 'Replica123!');
    final paths = <String>[];
    final client = MockClient((request) async {
      paths.add('${request.method} ${request.url.path}');
      expect(request.headers['Authorization'], 'Bearer synthetic-test-token');
      expect(request.body, isEmpty);
      final job = {
        'id': 'job',
        'target_id': 'record',
        'target_type': 'record',
        'status': request.method == 'DELETE' ? 'pending' : 'completed',
        'remaining_steps': request.method == 'DELETE' ? ['storage'] : [],
      };
      return http.Response(
        jsonEncode(
          request.url.path == '/deletion-jobs'
              ? {
                  'items': [job],
                  'limit': 100,
                  'offset': 0,
                }
              : job,
        ),
        request.method == 'DELETE' ? 202 : 200,
      );
    });
    addTearDown(client.close);
    final api = ApiRecordService(
      client: client,
      baseUrl: Uri.parse('https://example.invalid'),
      auth: auth,
      personaId: 'p',
      policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'v'),
      fileTransport: Transfer(),
    );
    final c = RecordDeletionController(service: api, targetId: 'record');
    addTearDown(c.dispose);
    expect(await c.execute(), isFalse);
    expect(await c.execute(), isTrue);
    expect((await api.deletionJobs()).length, 1);
    expect(paths, [
      'DELETE /records/record',
      'GET /deletion-jobs/job',
      'GET /deletion-jobs',
    ]);
    expect(api.canDeleteAccount, isFalse);
    expect(api.canRetryDeletion, isFalse);
  });
}
