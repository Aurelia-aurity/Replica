import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/records/api_record_service.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/record_speaker_check.dart';
import 'package:mobile/records/records_panel.dart';
import 'package:mobile/services/backend_service.dart';

import 'records_test.dart' show ApiTestAuth;

final _waiting = RecordEntry(
  id: 'r1',
  name: 'kakao.txt',
  kind: RecordKind.text,
  status: RecordStatus.uploading,
  createdAt: DateTime.utc(2026, 11, 27),
  errorCode: 'speaker_required',
);

BackendFailure _speakerError(
  String code,
  List<String> speakers, {
  String field = 'target_speaker',
}) => BackendFailure(
  BackendFailureKind.generic,
  code: code,
  retryable: true,
  statusCode: 422,
  field: field,
  speakers: speakers,
);

/// A record waiting for its speakers, and scripted `/uploaded` answers.
class _Waiting extends PreviewRecordService {
  final calls = <Map<String, String?>?>[];

  /// Each call takes the next answer: a failure to throw, or null for 200.
  final answers = <BackendFailure?>[];
  var entries = [_waiting];

  @override
  Future<List<RecordEntry>> list() async => entries;

  @override
  Future<void> completeUpload(
    String recordId, {
    Map<String, String?>? speakers,
  }) async {
    expect(recordId, _waiting.id);
    calls.add(speakers);
    final answer = answers.isEmpty ? null : answers.removeAt(0);
    if (answer != null) throw answer;
  }
}

void main() {
  group('RecordEntry', () {
    test('a speaker error waiting in uploading needs a check', () {
      expect(_waiting.needsSpeakerCheck, isTrue);
      expect(_waiting.statusLabel, '확인 필요');
      // It waits for the user, so the list does not keep polling for it.
      expect(_waiting.inProgress, isFalse);
      final uploading = RecordEntry.fromJson({
        'id': 'r2',
        'original_name': 'a.txt',
        'kind': 'text',
        'status': 'uploading',
        'created_at': '2026-11-27T00:00:00Z',
      });
      expect(uploading.needsSpeakerCheck, isFalse);
      expect(uploading.statusLabel, '반영 중');
      expect(uploading.inProgress, isTrue);
      final parsed = RecordEntry.fromJson({
        'id': 'r3',
        'original_name': 'a.txt',
        'kind': 'text',
        'status': 'uploading',
        'error_code': 'speaker_not_found',
        'created_at': '2026-11-27T00:00:00Z',
      });
      expect(parsed.needsSpeakerCheck, isTrue);
      // Only an uploading record waits for speakers.
      final failed = RecordEntry.fromJson({
        'id': 'r4',
        'original_name': 'a.txt',
        'kind': 'text',
        'status': 'failed',
        'error_code': 'speaker_required',
        'created_at': '2026-11-27T00:00:00Z',
      });
      expect(failed.needsSpeakerCheck, isFalse);
    });
  });

  group('SpeakerCheckController', () {
    late _Waiting service;
    late SpeakerCheckController check;
    setUp(() {
      service = _Waiting();
      check = SpeakerCheckController(service, _waiting);
    });
    tearDown(() => check.dispose());

    test('asks without a body, then sends the choice', () async {
      service.answers.add(_speakerError('SPEAKER_REQUIRED', ['엄마', '나']));
      await check.load();
      expect(service.calls, [null]);
      expect(check.analysis!.speakers.map((s) => s.name), ['엄마', '나']);
      expect(check.canSend, isFalse);
      check.selectTarget('엄마');
      check.selectProvider('엄마'); // The same person cannot be both.
      expect(check.providerSpeaker, isNull);
      check.selectProvider('나');
      expect(check.canSend, isTrue);
      await check.send();
      expect(service.calls.last, {
        'target_speaker': '엄마',
        'provider_speaker': '나',
      });
      expect(check.done, isTrue);
    });

    test('a lone speaker is chosen only by the user', () async {
      service.answers.add(_speakerError('SPEAKER_REQUIRED', ['엄마']));
      await check.load();
      expect(check.singleSpeaker, isTrue);
      expect(check.targetSpeaker, isNull);
      expect(check.canSend, isFalse);
      check.selectProvider('엄마');
      expect(check.providerSpeaker, isNull);
      check.selectTarget('엄마');
      await check.send();
      expect(service.calls.last, {
        'target_speaker': '엄마',
        'provider_speaker': null,
      });
      expect(check.done, isTrue);
    });

    test('a value the server does not allow is cleared at once', () async {
      service.answers.add(
        _speakerError('SPEAKER_NOT_ALLOWED', ['엄마'], field: 'provider_speaker'),
      );
      await check.load();
      // Leaving target out keeps the reserved one; null clears the provider.
      expect(service.calls, [
        null,
        {'provider_speaker': null},
      ]);
      expect(check.done, isTrue);
    });

    test('a choice the server rejects again shows its new list', () async {
      service.answers.addAll([
        _speakerError('SPEAKER_REQUIRED', ['엄마', '나']),
        _speakerError('SPEAKER_NOT_FOUND', ['엄마', '아빠']),
      ]);
      await check.load();
      check.selectTarget('엄마');
      check.selectProvider('나');
      await check.send();
      expect(check.done, isFalse);
      expect(check.analysis!.speakers.map((s) => s.name), ['엄마', '아빠']);
      expect(check.targetSpeaker, '엄마');
      expect(check.providerSpeaker, isNull);
    });

    test(
      'a record that cannot continue says so and can be checked again',
      () async {
        service.answers.add(
          const BackendFailure(
            BackendFailureKind.generic,
            code: 'CONFLICT',
            statusCode: 409,
          ),
        );
        await check.load();
        expect(check.analysis, isNull);
        expect(check.error, contains('삭제하고 새로 올려'));
        await check.load();
        expect(service.calls, [null, null]);
        expect(check.done, isTrue);
      },
    );
  });

  testWidgets('SC-03 shows 확인 필요 and continues with the server speakers', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(800, 2000);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final service = _Waiting()
      ..answers.add(_speakerError('SPEAKER_REQUIRED', ['엄마', '나']));
    final controller = RecordController(service);
    addTearDown(controller.dispose);
    await tester.runAsync(controller.refresh);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: RecordsPanel(controller: controller)),
      ),
    );
    await tester.pump();
    expect(find.text('확인 필요'), findsOneWidget);
    // Nothing is asked of the server until the user opens the record.
    expect(service.calls, isEmpty);
    await tester.tap(find.text('kakao.txt'));
    await tester.pumpAndSettle();
    expect(find.byType(RecordSpeakerCheckScreen), findsOneWidget);
    expect(service.calls, [null]);
    expect(find.text('누구의 말투를 반영할까요?'), findsOneWidget);
    expect(find.textContaining('메시지 '), findsNothing);
    FilledButton send() => tester.widget<FilledButton>(
      find.byKey(const ValueKey('speaker-check-send')),
    );
    expect(send().onPressed, isNull);
    await tester.tap(find.byKey(const ValueKey('speaker-target-엄마')));
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('speaker-provider-나')));
    await tester.pump();
    service.entries = [
      RecordEntry(
        id: _waiting.id,
        name: _waiting.name,
        kind: RecordKind.text,
        status: RecordStatus.uploaded,
        createdAt: _waiting.createdAt,
      ),
    ];
    await tester.tap(find.byKey(const ValueKey('speaker-check-send')));
    await tester.pumpAndSettle();
    expect(find.textContaining('화자 확인을 마쳤어요'), findsOneWidget);
    // The answer was used once; the screen did not ask again.
    expect(service.calls, hasLength(2));
    await tester.tap(find.byKey(const ValueKey('speaker-check-send')));
    await tester.pumpAndSettle();
    expect(find.byType(RecordSpeakerCheckScreen), findsNothing);
    expect(find.text('확인 필요'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  test('the API calls /uploaded with or without speakers', () async {
    final auth = ApiTestAuth();
    addTearDown(auth.dispose);
    await auth.signIn('preview.a@example.com', PreviewAuthService.password);
    final seen = <http.Request>[];
    final client = MockClient((request) async {
      seen.add(request);
      return http.Response(
        jsonEncode({
          'id': 'r1',
          'original_name': 'kakao.txt',
          'kind': 'text',
          'status': 'uploaded',
          'created_at': '2026-11-27T00:00:00Z',
        }),
        200,
        headers: {'content-type': 'application/json'},
      );
    });
    addTearDown(client.close);
    final RecordService api = ApiRecordService(
      client: client,
      baseUrl: Uri.parse('https://api.invalid'),
      auth: auth,
      personaId: 'persona',
      policy: const RecordPolicy(maxBytes: 1000, consentVersion: 'test'),
      fileTransport: _NoTransfer(),
    );
    await api.completeUpload('r1');
    await api.completeUpload(
      'r1',
      speakers: {'target_speaker': '엄마', 'provider_speaker': null},
    );
    expect(seen.map((r) => '${r.method} ${r.url.path}'), [
      'POST /records/r1/uploaded',
      'POST /records/r1/uploaded',
    ]);
    expect(seen.first.body, isEmpty);
    expect(jsonDecode(seen.last.body), {
      'target_speaker': '엄마',
      'provider_speaker': null,
    });
  });
}

class _NoTransfer extends RecordFileTransport {
  @override
  Future<void> ensureUploaded(RecordUpload upload) =>
      throw StateError('No transfer in this test');
}
