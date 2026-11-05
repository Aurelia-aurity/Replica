import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_review.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/record_review_screen.dart';
import 'package:mobile/records/records_panel.dart';
import 'package:mobile/records/api_record_service.dart';
import 'package:mobile/services/backend_service.dart';

import 'records_test.dart' show ApiTestAuth, Transfer;

// Fixtures never contain user file contents.
class ReviewFixture extends PreviewRecordService {
  RecordStatus status = RecordStatus.ready;
  bool saved = false, confirmed = false, failSave = false, failConfirm = false;
  String body = '합성 전사문입니다.';
  int confirms = 0;
  Completer<void>? pending;
  DateTime updated = DateTime.utc(2026, 10, 30);
  RecordEntry get entry => RecordEntry(
    id: 'record',
    name: '합성 음성.m4a',
    kind: RecordKind.audio,
    status: status,
    createdAt: updated,
    confirmed: confirmed,
  );
  TextVersion get version => TextVersion(
    id: 'version',
    recordId: 'record',
    revision: 1,
    content: body,
    status: confirmed ? 'confirmed' : 'draft',
    updatedAt: updated,
    confirmedAt: confirmed ? updated : null,
  );
  @override
  Future<List<RecordEntry>> list() async => [entry];
  @override
  Future<RecordReview> review(String recordId) async =>
      RecordReview(record: entry, version: version);
  @override
  Future<TextVersion> saveText(TextVersion v, String content) async {
    if (pending != null) await pending!.future;
    if (failSave) {
      throw const BackendFailure(BackendFailureKind.generic, code: 'CONFLICT');
    }
    saved = true;
    body = content;
    updated = updated.add(const Duration(seconds: 1));
    return version;
  }

  @override
  Future<TextVersion> confirmText(TextVersion v) async {
    confirms++;
    if (failConfirm) throw StateError('synthetic failure');
    confirmed = true;
    return version;
  }
}

void main() {
  for (final reloadBeforeClosing in [false, true]) {
    test('reopening preserves unresolved edits (reload: $reloadBeforeClosing)', () async {
      final service = ReviewFixture()..failSave = true;
      final drafts = <String, RecordReviewDraft>{};
      final first = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: drafts,
      );
      await first.load();
      first.edit('내 수정');
      expect(await first.save(confirm: true), isFalse);
      service.body = '다른 기기에서 저장한 내용';
      service.updated = service.updated.add(const Duration(minutes: 1));
      service.failSave = false;
      if (reloadBeforeClosing) {
        await first.load();
        expect(first.needsResolution, isTrue);
        // Refreshing again must not implicitly accept the changed server text.
        await first.load();
        expect(first.needsResolution, isTrue);
      }
      first.dispose();

      final reopened = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: drafts,
      );
      addTearDown(reopened.dispose);
      await reopened.load();
      expect(reopened.content, '내 수정');
      expect(reopened.needsResolution, isTrue);
      expect(reopened.canConfirm, isFalse);
      expect(await reopened.save(), isFalse);
      expect(await reopened.save(confirm: true), isFalse);
      expect(service.saved, isFalse);
      reopened.useServer();
      expect(reopened.content, service.body);
      expect(drafts, isEmpty);
      expect(reopened.canConfirm, isTrue);
    });
  }

  test(
    'accepted server base survives reopening and detects further changes',
    () async {
      final service = ReviewFixture();
      final drafts = <String, RecordReviewDraft>{};
      final first = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: drafts,
      );
      await first.load();
      first.edit('내 수정');
      service.body = '외부 수정';
      service.updated = service.updated.add(const Duration(minutes: 1));
      await first.load();
      expect(first.needsResolution, isTrue);
      first.keepMine();
      first.dispose();

      final reopened = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: drafts,
      );
      addTearDown(reopened.dispose);
      await reopened.load();
      expect(reopened.content, '내 수정');
      expect(reopened.canConfirm, isTrue);
      service.body = '또 다른 외부 수정';
      service.updated = service.updated.add(const Duration(minutes: 1));
      await reopened.load();
      expect(reopened.needsResolution, isTrue);
      reopened.keepMine();
      expect(await reopened.save(confirm: true), isTrue);
      expect(service.body, '내 수정');
      expect(drafts, isEmpty);
    },
  );

  test(
    'reopening after a saved edit and failed confirmation needs no resolution',
    () async {
      final service = ReviewFixture()..failConfirm = true;
      final drafts = <String, RecordReviewDraft>{};
      final first = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: drafts,
      );
      await first.load();
      first.edit('이미 저장된 내 수정');
      expect(await first.save(confirm: true), isFalse);
      first.dispose();
      final reopened = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: drafts,
      );
      addTearDown(reopened.dispose);
      await reopened.load();
      expect(reopened.needsResolution, isFalse);
      expect(reopened.canConfirm, isTrue);
      service.failConfirm = false;
      expect(await reopened.save(confirm: true), isTrue);
    },
  );

  testWidgets('leaving a conflict and reopening still requires a choice', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(800, 1800);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final service = ReviewFixture()..failSave = true;
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
    await tester.tap(find.byKey(const ValueKey('segment-text-0')));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('segment-editor-0')),
      '내 수정',
    );
    await tester.tap(find.text('확정하기'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('나중에'));
    await tester.pumpAndSettle();
    service.body = '외부 수정';
    service.updated = service.updated.add(const Duration(minutes: 1));
    service.failSave = false;
    await tester.tap(find.text(service.entry.name));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('conflict-resolution')), findsOneWidget);
    expect(
      tester
          .widget<FilledButton>(
            find.byKey(const ValueKey('transcript-confirm')),
          )
          .onPressed,
      isNull,
    );
    expect(service.saved, isFalse);
    await tester.tap(find.byKey(const ValueKey('resolve-mine')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('확정하기'));
    await tester.pumpAndSettle();
    expect(service.body, '내 수정');
    expect(service.confirmed, isTrue);
  });

  test(
    'edited text retained across conflict and reload, no automatic overwrite',
    () async {
      final service = ReviewFixture()..failSave = true;
      final drafts = <String, RecordReviewDraft>{};
      final c = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: drafts,
      );
      addTearDown(c.dispose);
      await c.load();
      c.edit('수정한 합성 내용');
      expect(await c.save(confirm: true), isFalse);
      expect(c.content, '수정한 합성 내용');
      expect(c.conflict, isTrue);
      expect(c.canConfirm, isFalse);
      expect(service.confirms, 0);
      service.failSave = false;
      await c.load();
      expect(c.content, '수정한 합성 내용');
      expect(await c.save(confirm: true), isTrue);
      expect(drafts, isEmpty);
      expect(c.confirmed, isTrue);
      expect(c.value!.profile, ReviewProfileState.unknown);
    },
  );
  test(
    'confirm failure keeps saved edits and retry does not save twice',
    () async {
      final service = ReviewFixture()..failConfirm = true;
      final c = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: {},
      );
      addTearDown(c.dispose);
      await c.load();
      c.edit('수정');
      expect(await c.save(confirm: true), isFalse);
      expect(c.value!.version!.content, '수정');
      expect(c.confirmed, isFalse);
      service.failConfirm = false;
      expect(await c.save(confirm: true), isTrue);
      expect(service.confirms, 2);
    },
  );
  test(
    'double save blocked and disposed controller ignores delayed result',
    () async {
      final service = ReviewFixture()..pending = Completer<void>();
      final c = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: {},
      );
      await c.load();
      c.edit('수정');
      final save = c.save(confirm: true);
      expect(await c.save(confirm: true), isFalse);
      c.dispose();
      service.pending!.complete();
      await save;
      expect(service.confirms, 0);
    },
  );
  test(
    'processing and empty text cannot confirm; unconfirmed is not reflected',
    () async {
      final service = ReviewFixture()..status = RecordStatus.processing;
      final c = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: {},
      );
      addTearDown(c.dispose);
      await c.load();
      expect(c.canConfirm, isFalse);
      service.status = RecordStatus.ready;
      await c.load();
      c.edit('  ');
      expect(c.canConfirm, isFalse);
      expect(service.entry.withTextConfirmed().statusLabel, '확정됨');
    },
  );
  testWidgets(
    'later preserves edits, reopens and confirms, detail is read only',
    (tester) async {
      final service = ReviewFixture();
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
      await tester.tap(find.byKey(const ValueKey('segment-text-0')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('segment-editor-0')),
        '검토한 합성 전사문',
      );
      await tester.tap(find.text('나중에'));
      await tester.pumpAndSettle();
      expect(service.body, '검토한 합성 전사문');
      expect(service.confirmed, isFalse);
      await tester.tap(find.text(service.entry.name));
      await tester.pumpAndSettle();
      expect(find.text('검토한 합성 전사문'), findsOneWidget);
      await tester.tap(find.text('확정하기'));
      await tester.pumpAndSettle();
      expect(service.confirmed, isTrue);
      await tester.tap(find.text(service.entry.name));
      await tester.pumpAndSettle();
      expect(find.text('기록 상세'), findsOneWidget);
      expect(find.byType(TextField), findsNothing);
      expect(
        find.text('전사문은 확정됐어요. 이 기록의 프로필·검색 색인 반영 상태는 확인이 필요해요.'),
        findsOneWidget,
      );
    },
  );
  testWidgets(
    'small screen keyboard and failed transcription keep navigation usable',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(320, 568);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      final service = ReviewFixture()..status = RecordStatus.failed;
      final records = RecordController(service);
      addTearDown(records.dispose);
      await tester.pumpWidget(
        MaterialApp(
          home: RecordReviewScreen(records: records, entry: service.entry),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('전사에 실패했어요'), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('transcript-confirm')),
            )
            .onPressed,
        isNull,
      );
      expect(find.byType(BackButton), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
  test(
    'draft API uses optimistic locking and reconciles lost confirmation',
    () async {
      final auth = ApiTestAuth();
      addTearDown(auth.dispose);
      await auth.signIn('preview.a@example.com', 'Replica123!');
      var content = '합성';
      var status = 'draft';
      var confirms = 0;
      var patches = 0;
      final updated = DateTime.utc(2026, 10, 30);
      Map<String, Object?> version() => {
        'id': 'version',
        'record_id': 'record',
        'revision': 1,
        'content': content,
        'status': status,
        'updated_at': updated.toIso8601String(),
        'confirmed_at': status == 'confirmed'
            ? updated.toIso8601String()
            : null,
      };
      final client = MockClient((request) async {
        expect(request.headers['Authorization'], 'Bearer synthetic-test-token');
        if (request.method == 'PATCH') {
          patches++;
          final body = jsonDecode(request.body) as Map;
          expect(body.keys.toSet(), {'content', 'expected_updated_at'});
          expect(body['expected_updated_at'], updated.toIso8601String());
          content = body['content'] as String;
          return http.Response(
            jsonEncode(version()),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }
        if (request.method == 'POST') {
          confirms++;
          status = 'confirmed';
          throw http.ClientException('lost response');
        }
        if (request.url.path.endsWith('text-versions')) {
          return http.Response(
            jsonEncode({
              'items': [version()],
            }),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }
        if (request.url.path.endsWith('style-profile')) {
          return http.Response('null', 200);
        }
        return http.Response(
          jsonEncode({
            'id': 'record',
            'original_name': '합성.wav',
            'kind': 'audio',
            'status': 'ready',
            'created_at': updated.toIso8601String(),
          }),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      });
      addTearDown(client.close);
      final api = ApiRecordService(
        client: client,
        baseUrl: Uri.parse('https://example.invalid'),
        auth: auth,
        personaId: 'persona',
        policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
        fileTransport: Transfer(),
      );
      var v = (await api.review('record')).version!;
      v = await api.saveText(v, '수정');
      await expectLater(api.confirmText(v), throwsA(isA<BackendFailure>()));
      v = await api.confirmText(v);
      expect(v.status, 'confirmed');
      expect(confirms, 1);
      expect(patches, 1);
      // A confirmed transcript does not establish profile/index completion.
      expect((await api.review('record')).profile, ReviewProfileState.unknown);
    },
  );
}
