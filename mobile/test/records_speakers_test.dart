import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/record_upload_screen.dart';
import 'package:mobile/records/text_analysis.dart';

RecordFile _text(String body, {String name = 'kakao.txt'}) {
  final bytes = utf8.encode(body);
  return RecordFile(
    name: name,
    sizeBytes: bytes.length,
    openRead: () => Stream.value(bytes),
  );
}

const _android = '''엄마 님과 카카오톡 대화
저장한 날짜 : 2025-09-20 15:00:00

--------------- 2025년 9월 20일 토요일 ---------------
[엄마] [오후 3:12] 밥은 먹었니?
[나] [오후 3:13] 네 먹었어요
그리고 곧 갈게요
[엄마] [오후 3:14] 조심히 와
엄마님이 들어왔습니다.
''';

const _ios = '''2025. 9. 20. 오후 3:12, 엄마 : 밥은 먹었니?
2025. 9. 20. 오후 3:13, 나 : 네
2025년 9월 20일 오후 3:14, 엄마 : 응
2025-09-20 15:15, 나 : 곧 갈게
''';

void main() {
  group('analyzeTextRecord', () {
    test(
      'reads an Android/PC export and counts messages per speaker',
      () async {
        final result = await analyzeTextRecord(_text(_android));
        expect(result.kakao, isTrue);
        expect(result.label, '카카오톡 대화로 인식됨');
        expect(result.messages, 3);
        expect(
          {for (final s in result.speakers) s.name: s.messages},
          {'엄마': 2, '나': 1},
        );
        // Most active speaker first.
        expect(result.speakers.first.name, '엄마');
        expect(result.needsSpeakerSelection, isTrue);
      },
    );

    test('reads iOS and desktop style timestamps', () async {
      final result = await analyzeTextRecord(_text(_ios));
      expect(
        {for (final s in result.speakers) s.name: s.messages},
        {'엄마': 2, '나': 2},
      );
    });

    test('tolerates a byte order mark and malformed bytes', () async {
      final bom = await analyzeTextRecord(
        _text('﻿[엄마] [오후 3:12] 안녕\n[나] [오후 3:13] 응'),
      );
      expect(bom.speakers.length, 2);
      final broken = RecordFile(
        name: 'x.txt',
        sizeBytes: 5,
        openRead: () => Stream.value([0xff, 0xfe, 0x41, 0x0a, 0xff]),
      );
      expect((await analyzeTextRecord(broken)).kakao, isFalse);
    });

    test('plain text, one stray timestamp and audio are not chats', () async {
      expect(
        (await analyzeTextRecord(_text('오늘은 날이 좋았다.\n일기'))).kakao,
        isFalse,
      );
      expect(
        (await analyzeTextRecord(_text('[엄마] [오후 3:12] 하나뿐'))).kakao,
        isFalse,
      );
      final audio = RecordFile(
        name: 'a.m4a',
        sizeBytes: 3,
        openRead: () => throw StateError('audio is never read'),
      );
      expect((await analyzeTextRecord(audio)).kakao, isFalse);
      expect(TextAnalysis.plain.label, '일반 텍스트');
    });

    test('a one-person chat needs no speaker selection', () async {
      final solo = await analyzeTextRecord(
        _text('[나] [오후 3:12] 메모\n[나] [오후 3:13] 또 메모'),
      );
      expect(solo.kakao, isTrue);
      expect(solo.needsSpeakerSelection, isFalse);
    });

    test('thousands are grouped', () {
      expect(formatCount(1204), '1,204');
      expect(formatCount(12), '12');
      expect(formatCount(1234567), '1,234,567');
    });
  });

  group('controller speaker selection', () {
    test('upload waits for two different speakers on a chat export', () async {
      final controller = RecordController(PreviewRecordService());
      addTearDown(controller.dispose);
      controller.select(_text(_android));
      expect(controller.analyzing, isTrue);
      controller.setConsent(true);
      await pumpEventQueue();
      expect(controller.analyzing, isFalse);
      expect(controller.needsSpeakers, isTrue);
      expect(controller.canUpload, isFalse);
      controller.selectTarget('엄마');
      expect(controller.canUpload, isFalse);
      // The same person cannot be both.
      controller.selectProvider('엄마');
      expect(controller.providerSpeaker, isNull);
      controller.selectProvider('나');
      expect(controller.canUpload, isTrue);
      await controller.send();
      expect(controller.uploadSucceeded, isTrue);
      final upload = controller.attempt!;
      expect(upload.targetSpeaker, '엄마');
      expect(upload.providerSpeaker, '나');
      // Never part of the draft reservation unless the contract adds them.
      expect(
        upload.reservationJson('persona').containsKey('target_speaker'),
        isFalse,
      );
      expect(
        upload.reservationJson(
          'persona',
          includeSpeakers: true,
        )['target_speaker'],
        '엄마',
      );
    });

    test('plain text uploads without a speaker step', () async {
      final controller = RecordController(PreviewRecordService());
      addTearDown(controller.dispose);
      controller.select(_text('오늘의 일기', name: 'diary.txt'));
      controller.setConsent(true);
      await controller.send();
      expect(controller.uploadSucceeded, isTrue);
      expect(controller.attempt!.targetSpeaker, isNull);
    });

    test('a file that cannot be read still uploads as a whole file', () async {
      final controller = RecordController(
        PreviewRecordService(),
        analyze: (_) async => throw StateError('unreadable'),
      );
      addTearDown(controller.dispose);
      controller.select(_text('내용'));
      controller.setConsent(true);
      await controller.send();
      expect(controller.analysisError, isNotNull);
      expect(controller.uploadSucceeded, isTrue);
    });

    test(
      'changing the file clears speakers and a late analysis is ignored',
      () async {
        final controller = RecordController(PreviewRecordService());
        addTearDown(controller.dispose);
        controller.select(_text(_android));
        await pumpEventQueue();
        controller.selectTarget('엄마');
        controller.select(_text('일기', name: 'diary.txt'));
        expect(controller.targetSpeaker, isNull);
        expect(controller.analysis, isNull);
        await pumpEventQueue();
        expect(controller.needsSpeakers, isFalse);
        controller.beginSelection();
        expect(controller.analysis, isNull);
      },
    );

    test('names are dropped from memory when local data is cleared', () async {
      final controller = RecordController(PreviewRecordService());
      addTearDown(controller.dispose);
      controller.select(_text(_android));
      await pumpEventQueue();
      controller.selectTarget('엄마');
      controller.clearLocalData();
      expect(controller.analysis, isNull);
      expect(controller.targetSpeaker, isNull);
      expect(controller.selected, isNull);
    });
  });

  testWidgets('SC-04 shows speakers, counts and blocks the same person', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(800, 2000);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final controller = RecordController(PreviewRecordService());
    addTearDown(controller.dispose);
    controller.select(_text(_android));
    await tester.pumpWidget(
      MaterialApp(
        home: RecordUploadScreen(
          controller: controller,
          picker: () async => null,
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.textContaining('카카오톡 대화로 인식됨'), findsOneWidget);
    expect(find.text('누구의 말투를 반영할까요?'), findsOneWidget);
    expect(find.text('이 기록을 제공한 사람은 누구인가요?'), findsOneWidget);
    expect(find.text('메시지 2개'), findsNWidgets(2));
    expect(find.text('메시지 1개'), findsNWidgets(2));
    expect(
      tester
          .widget<FilledButton>(find.byKey(const ValueKey('record-upload')))
          .onPressed,
      isNull,
    );
    await tester.tap(find.byKey(const ValueKey('speaker-target-엄마')));
    await tester.pump();
    expect(
      tester
          .widget<InkWell>(find.byKey(const ValueKey('speaker-provider-엄마')))
          .onTap,
      isNull,
    );
    await tester.tap(find.byKey(const ValueKey('speaker-provider-나')));
    await tester.pump();
    await tester.tap(find.byType(Checkbox));
    await tester.pump();
    expect(
      tester
          .widget<FilledButton>(find.byKey(const ValueKey('record-upload')))
          .onPressed,
      isNotNull,
    );
    expect(tester.takeException(), isNull);
  });
}
