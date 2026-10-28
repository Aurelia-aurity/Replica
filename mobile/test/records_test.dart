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
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/record_upload_screen.dart';
import 'package:mobile/records/records_panel.dart';
import 'package:mobile/services/backend_service.dart';

RecordFile file(String name, [int size = 128]) => RecordFile(
  name: name,
  sizeBytes: size,
  openRead: () => Stream.value(List.filled(size, 65)),
);

class ControlledRecords extends PreviewRecordService {
  Completer<void>? pending;
  final attempts = <RecordUpload>[];
  bool failList = false;
  @override
  Future<List<RecordEntry>> list() async {
    if (failList) throw StateError('synthetic');
    return super.list();
  }

  @override
  Future<void> upload(RecordUpload upload, void Function() changed) async {
    attempts.add(upload);
    if (pending != null) await pending!.future;
    return super.upload(upload, changed);
  }
}

class Transfer extends RecordFileTransport {
  int calls = 0;
  @override
  Future<void> ensureUploaded(RecordUpload upload) async {
    calls++;
  }
}

class ApiTestAuth extends PreviewAuthService {
  @override
  Future<Map<String, String>> authorizationHeaders() async => {
    'Authorization': 'Bearer synthetic-test-token',
  };
}

Map<String, Object?> recordJson(String id, {String status = 'uploading'}) => {
  'id': id,
  'original_name': 'synthetic.txt',
  'kind': 'text',
  'status': status,
  'size_bytes': 128,
  'created_at': '2026-10-28T00:00:00Z',
};

void main() {
  test('format, empty file, filename and exact size boundary validation', () {
    const policy = RecordPolicy(maxBytes: 128, consentVersion: 'test');
    for (final name in [
      'sample.txt',
      'sample.WAV',
      'sample.mp3',
      'sample.m4a',
    ]) {
      expect(policy.validate(file(name)), isNull);
    }
    for (final item in [
      file('x.pdf'),
      file('x'),
      file('x.txt', 0),
      file('x.txt', 129),
      file('${'x' * 256}.txt'),
    ]) {
      expect(policy.validate(item), isNotNull);
    }
  });

  test('ready draft record never claims confirmed personalization', () {
    final record = RecordEntry.fromJson(recordJson('id', status: 'ready'));
    expect(record.statusLabel, '확인 필요');
    expect(record.confirmed, isFalse);
  });

  test(
    'consent gating, double submit, stable ID retry, one list entry',
    () async {
      final service = ControlledRecords()..pending = Completer<void>();
      final controller = RecordController(service);
      addTearDown(controller.dispose);
      controller.select(file('synthetic.txt'));
      await controller.send();
      expect(service.attempts, isEmpty);
      controller.setConsent(true);
      final first = controller.send();
      await controller.send();
      expect(service.attempts.length, 1);
      service.pending!.completeError(StateError('synthetic network error'));
      await first;
      expect(controller.uploadError, isNotNull);
      final id = controller.attempt!.id;
      service.pending = null;
      await controller.send();
      await controller.send();
      expect(service.attempts.map((e) => e.id), [id, id]);
      expect(controller.entries.length, 1);
      expect(controller.uploadSucceeded, isTrue);
      expect(controller.attempt!.consentAt.isUtc, isFalse);
      expect(
        controller.attempt!.reservationJson('persona')['consent_at'],
        endsWith('Z'),
      );
      controller.beginSelection();
      expect(controller.consent, isFalse);
    },
  );

  test('list error recovers; disposal ignores late notifications', () async {
    final service = ControlledRecords()..failList = true;
    final controller = RecordController(service);
    await controller.refresh();
    expect(controller.listError, isNotNull);
    service.failList = false;
    await controller.refresh();
    expect(controller.listError, isNull);
    service.pending = Completer<void>();
    controller.select(file('synthetic.txt'));
    controller.setConsent(true);
    final upload = controller.send();
    controller.dispose();
    service.pending!.complete();
    await upload;
  });

  test('draft adapter reconciles lost reservation and completion without duplicates', () async {
    final auth = ApiTestAuth();
    addTearDown(auth.dispose);
    await auth.signIn('preview.a@example.com', PreviewAuthService.password);
    final transfer = Transfer();
    var reservations = 0;
    var completions = 0;
    var status = 'uploading';
    String? savedId;
    final client = MockClient((request) async {
      if (request.method == 'POST' && request.url.path == '/records') {
        reservations++;
        final body = jsonDecode(request.body) as Map;
        savedId = body['record_id'] as String;
        expect(body['persona_id'], 'persona');
        expect(body.containsKey('user_id'), isFalse);
        expect(body['mime_type'], 'text/plain');
        throw http.ClientException('lost reservation response');
      }
      if (request.method == 'POST') {
        completions++;
        status = 'uploaded';
        throw http.ClientException('lost completion response');
      }
      return http.Response(
        jsonEncode(recordJson(savedId!, status: status)),
        200,
      );
    });
    addTearDown(client.close);
    final service = ApiRecordService(
      client: client,
      baseUrl: Uri.parse('https://example.invalid'),
      auth: auth,
      personaId: 'persona',
      policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
      fileTransport: transfer,
    );
    final upload = RecordUpload(
      file: file('synthetic.txt'),
      consentAt: DateTime.now(),
      consentVersion: 'test',
    );
    await expectLater(
      service.upload(upload, () {}),
      throwsA(isA<BackendFailure>()),
    );
    await expectLater(
      service.upload(upload, () {}),
      throwsA(isA<BackendFailure>()),
    );
    await service.upload(upload, () {});
    expect(reservations, 1);
    expect(transfer.calls, 1);
    expect(completions, 1);
    expect(upload.stage, UploadStage.done);
  });

  test('draft API pagination loads every page and filters deleting', () async {
    final auth = ApiTestAuth();
    addTearDown(auth.dispose);
    await auth.signIn('preview.a@example.com', PreviewAuthService.password);
    final offsets = <String>[];
    final client = MockClient((request) async {
      offsets.add(request.url.queryParameters['offset']!);
      expect(request.url.queryParameters['persona_id'], 'persona');
      final offset = int.parse(offsets.last);
      return http.Response(
        jsonEncode({
          'items': offset == 0
              ? List.generate(100, (i) => recordJson('$i'))
              : [recordJson('last'), recordJson('deleted', status: 'deleting')],
          'offset': offset,
          'limit': 100,
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
      policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
      fileTransport: Transfer(),
    );
    expect((await service.list()).length, 101);
    expect(offsets, ['0', '100']);
  });

  testWidgets(
    'empty list opens upload, cancel picker, invalid file and consent gated success',
    (tester) async {
      final controller = RecordController(PreviewRecordService());
      addTearDown(controller.dispose);
      var picks = 0;
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: RecordsPanel(
              controller: controller,
              picker: () async => switch (picks++) {
                0 => null,
                1 => file('wrong.pdf'),
                _ => file('synthetic.txt'),
              },
            ),
          ),
        ),
      );
      await tester.tap(find.text('첫 기록 올리기'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('파일 선택'));
      await tester.pumpAndSettle();
      expect(controller.selected, isNull);
      await tester.tap(find.text('파일 선택'));
      await tester.pumpAndSettle();
      expect(find.textContaining('지원하지 않는 형식'), findsOneWidget);
      await tester.tap(find.text('파일 선택'));
      await tester.pumpAndSettle();
      final button = find.byKey(const ValueKey('record-upload'));
      await tester.scrollUntilVisible(button, 150);
      expect(tester.widget<FilledButton>(button).onPressed, isNull);
      await tester.ensureVisible(find.byKey(const ValueKey('record-consent')));
      await tester.tap(find.byType(Checkbox));
      await tester.pumpAndSettle();
      await tester.ensureVisible(button);
      await tester.tap(button);
      await tester.pumpAndSettle();
      expect(find.text('기록이 등록됐어요.'), findsOneWidget);
      await tester.tap(find.text('기록 목록 보기'));
      await tester.pumpAndSettle();
      expect(find.text('synthetic.txt'), findsOneWidget);
      expect(find.text('확인 필요'), findsOneWidget);
      expect(find.text('텍스트 1'), findsOneWidget);
      await tester.tap(find.text('음성 0'));
      await tester.pumpAndSettle();
      expect(find.text('이 유형의 기록이 없어요.'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('upload fits a 320px screen with long filename and large text', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 568);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final controller = RecordController(PreviewRecordService());
    addTearDown(controller.dispose);
    controller.select(file('${'합성기록' * 25}.m4a'));
    await tester.pumpWidget(
      MaterialApp(
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: const TextScaler.linear(1.5)),
          child: child!,
        ),
        home: RecordUploadScreen(
          controller: controller,
          picker: () async => null,
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.byKey(const ValueKey('record-upload')),
      150,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('record drafts and list clear on logout and account switch', (
    tester,
  ) async {
    final auth = PreviewAuthService();
    addTearDown(auth.dispose);
    await auth.signIn('preview.a@example.com', PreviewAuthService.password);
    await tester.pumpWidget(ReplicaApp(authService: auth));
    await tester.pumpAndSettle();
    await tester.tap(find.text('기록'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('올리기'));
    await tester.pumpAndSettle();
    expect(find.text('자료 사용 동의·권한 확인'), findsOneWidget);
    await auth.signOut();
    await tester.pumpAndSettle();
    expect(find.text('기록 올리기'), findsNothing);
    await auth.signIn('preview.b@example.com', PreviewAuthService.password);
    await tester.pumpAndSettle();
    await tester.tap(find.text('기록'));
    await tester.pumpAndSettle();
    expect(find.text('아직 올린 기록이 없어요.'), findsOneWidget);
  });
}
