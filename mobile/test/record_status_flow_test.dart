import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/records/api_record_service.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_review.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/records_panel.dart';

import 'records_test.dart' show ApiTestAuth, Transfer, file;

class _SavingPreview extends PreviewRecordService {
  Completer<void>? saving;
  bool fail = false;
  int confirmations = 0;

  @override
  Future<TextVersion> confirmText(TextVersion version) async {
    confirmations++;
    await saving?.future;
    if (fail) throw StateError('save failed');
    return super.confirmText(version);
  }
}

Future<void> _seed(RecordController records, String name) async {
  records.select(file(name));
  records.setConsent(true);
  await records.send();
}

void main() {
  testWidgets('text upload opens a read-only detail without confirmation', (
    tester,
  ) async {
    final records = RecordController(PreviewRecordService());
    addTearDown(records.dispose);
    await tester.runAsync(() => _seed(records, 'diary.txt'));
    expect(records.entries.single.statusLabel, '반영됨');
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: RecordsPanel(controller: records)),
      ),
    );
    await tester.tap(find.text('diary.txt'));
    await tester.pumpAndSettle();
    expect(find.text('기록 상세'), findsOneWidget);
    expect(find.byKey(const ValueKey('transcript-confirm')), findsNothing);
    expect(find.byType(TextField), findsNothing);
    expect(find.text('텍스트 기록을 저장했어요.'), findsOneWidget);
  });

  testWidgets(
    'audio requires confirmation and reflects only after saving succeeds',
    (tester) async {
      final service = _SavingPreview();
      final records = RecordController(service);
      addTearDown(records.dispose);
      await tester.runAsync(() => _seed(records, 'voice.m4a'));
      expect(records.entries.single.statusLabel, '확인 필요');
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: RecordsPanel(controller: records)),
        ),
      );
      await tester.tap(find.text('voice.m4a'));
      await tester.pumpAndSettle();
      expect(find.text('전사 확인'), findsOneWidget);
      service.saving = Completer<void>();
      service.fail = true;
      await tester.tap(find.byKey(const ValueKey('transcript-confirm')));
      await tester.pump();
      expect(find.text('반영 중'), findsOneWidget);
      expect(records.entries.single.statusLabel, '확인 필요');
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('transcript-confirm')),
            )
            .onPressed,
        isNull,
      );
      service.saving!.complete();
      await tester.pumpAndSettle();
      expect(find.textContaining('저장하지 못했어요'), findsOneWidget);
      expect(records.entries.single.statusLabel, '확인 필요');
      service.fail = false;
      service.saving = Completer<void>();
      await tester.tap(find.byKey(const ValueKey('transcript-confirm')));
      await tester.pump();
      expect(find.text('반영 중'), findsOneWidget);
      service.saving!.complete();
      await tester.pumpAndSettle();
      expect(records.entries.single.statusLabel, '반영됨');
      expect(service.confirmations, 2);
      await tester.tap(find.text('voice.m4a'));
      await tester.pumpAndSettle();
      expect(find.text('기록 상세'), findsOneWidget);
      expect(find.byKey(const ValueKey('transcript-confirm')), findsNothing);
    },
  );

  testWidgets('reflecting and transcribing rows have no tap action', (
    tester,
  ) async {
    final records = RecordController(PreviewRecordService());
    addTearDown(records.dispose);
    records.entries = [
      for (final kind in RecordKind.values)
        RecordEntry(
          id: kind.name,
          name: '${kind.name}.file',
          kind: kind,
          status: RecordStatus.uploading,
          createdAt: DateTime.utc(2026),
        ),
      RecordEntry(
        id: 'stt',
        name: 'stt.m4a',
        kind: RecordKind.audio,
        status: RecordStatus.processing,
        createdAt: DateTime.utc(2026),
      ),
      RecordEntry(
        id: 'saving',
        name: 'saving.m4a',
        kind: RecordKind.audio,
        status: RecordStatus.ready,
        createdAt: DateTime.utc(2026),
        personalization: ReviewProfileState.processing,
      ),
    ];
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: RecordsPanel(controller: records)),
      ),
    );
    for (final entry in records.entries) {
      final row = find
          .ancestor(of: find.text(entry.name), matching: find.byType(InkWell))
          .first;
      expect(tester.widget<InkWell>(row).onTap, isNull);
      await tester.tap(find.text(entry.name));
      await tester.pump();
      expect(find.text('전사 확인'), findsNothing);
      expect(find.text('기록 상세'), findsNothing);
    }
    expect(find.text('반영 중'), findsNWidgets(2));
    expect(find.text('전사 중'), findsNWidgets(2));
  });

  for (final kind in RecordKind.values) {
    testWidgets(
      'API $kind uses successful storage as the completion boundary',
      (tester) async {
        final auth = ApiTestAuth();
        addTearDown(auth.dispose);
        await auth.signIn('preview.a@example.com', 'Replica123!');
        var confirmed = false;
        final status = kind == RecordKind.text ? 'uploaded' : 'ready';
        final client = MockClient((request) async {
          expect(request.url.path.endsWith('style-profile'), isFalse);
          final record = {
            'id': 'r',
            'original_name':
                'record.${kind == RecordKind.text ? 'txt' : 'm4a'}',
            'kind': kind.name,
            'status': status,
            'created_at': '2026-11-10T00:00:00Z',
          };
          if (request.url.path == '/records') {
            return http.Response(
              jsonEncode({
                'items': [record],
              }),
              200,
            );
          }
          if (request.url.path == '/deletion-jobs') {
            return http.Response('{"items":[]}', 200);
          }
          final version = {
            'id': 'v',
            'record_id': 'r',
            'revision': 1,
            'content': 'synthetic text',
            'status': confirmed ? 'confirmed' : 'draft',
            'updated_at': '2026-11-10T00:00:00Z',
            'confirmed_at': confirmed ? '2026-11-10T00:00:00Z' : null,
          };
          if (request.method == 'POST') {
            confirmed = true;
            return http.Response(
              jsonEncode({
                ...version,
                'status': 'confirmed',
                'confirmed_at': '2026-11-10T00:00:00Z',
              }),
              200,
            );
          }
          if (request.url.path.endsWith('text-versions')) {
            return http.Response(
              jsonEncode({
                'items': [version],
              }),
              200,
            );
          }
          return http.Response(jsonEncode(record), 200);
        });
        addTearDown(client.close);
        final api = ApiRecordService(
          client: client,
          baseUrl: Uri.parse('https://example.invalid'),
          auth: auth,
          personaId: 'persona',
          fileTransport: Transfer(),
          policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
        );
        if (kind == RecordKind.audio) {
          expect((await api.list()).single.statusLabel, '확인 필요');
          await api.confirmText((await api.review('r')).version!);
        }
        final records = RecordController(api);
        addTearDown(records.dispose);
        await records.refresh();
        expect(records.entries.single.statusLabel, '반영됨');
        expect(records.entries.single.inProgress, isFalse);
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(body: RecordsPanel(controller: records)),
          ),
        );
        await tester.tap(find.text(records.entries.single.name));
        await tester.pumpAndSettle();
        expect(find.text('기록 상세'), findsOneWidget);
        expect(find.text('반영됨 · 기록을 저장했어요.'), findsOneWidget);
        expect(find.byKey(const ValueKey('transcript-confirm')), findsNothing);
        expect(find.textContaining('검색 색인 반영을 확인'), findsNothing);
      },
    );
  }
}
