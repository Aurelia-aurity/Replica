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
  test(
    'edited text retained across conflict and reload, no automatic overwrite',
    () async {
      final service = ReviewFixture()..failSave = true;
      final drafts = <String, String>{};
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
      await tester.enterText(
        find.byKey(const ValueKey('transcript-editor')),
        '검토한 합성 전사문',
      );
      await tester.tap(find.text('나중에'));
      await tester.pumpAndSettle();
      expect(service.body, '검토한 합성 전사문');
      expect(service.confirmed, isFalse);
      await tester.tap(find.text(service.entry.name));
      await tester.pumpAndSettle();
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        '검토한 합성 전사문',
      );
      await tester.tap(find.text('확정하기'));
      await tester.pumpAndSettle();
      expect(service.confirmed, isTrue);
      await tester.tap(find.text(service.entry.name));
      await tester.pumpAndSettle();
      expect(find.text('기록 상세'), findsOneWidget);
      expect(find.byType(TextField), findsNothing);
      expect(find.text('개인화 반영 상태를 아직 확인할 수 없어요.'), findsOneWidget);
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
      expect((await api.review('record')).profile, ReviewProfileState.unknown);
    },
  );
}
