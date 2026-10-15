import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/chat/chat_screen.dart';
import 'package:mobile/chat/chat_service.dart';

class ControlledService implements ChatService {
  final requests = <String>[];
  Completer<String> pending = Completer<String>();
  @override
  bool get isDemo => true;
  @override
  Future<String> reply(String message) {
    requests.add(message);
    return pending.future;
  }
}

void main() {
  testWidgets('SC-09 keeps AI disclosure fixed and labels every reply', (
    tester,
  ) async {
    final service = ControlledService();
    await tester.pumpWidget(MaterialApp(home: ChatScreen(service: service)));
    expect(find.text('엄마'), findsOneWidget);
    final disclosure = find.text('기록을 바탕으로 AI가 만든 대화예요');
    final initialPosition = tester.getTopLeft(disclosure);
    await tester.enterText(find.byType(TextField), '질문');
    await tester.pump();
    await tester.tap(find.byTooltip('메시지 전송'));
    service.pending.complete(List.filled(30, '긴 응답').join('\n'));
    await tester.pumpAndSettle();
    expect(find.text('AI 생성'), findsOneWidget);
    await tester.drag(find.byType(ListView), const Offset(0, 200));
    await tester.pumpAndSettle();
    expect(tester.getTopLeft(disclosure), initialPosition);
  });

  testWidgets('listen explains synthetic voice and demo limitation', (
    tester,
  ) async {
    final service = ControlledService();
    await tester.pumpWidget(MaterialApp(home: ChatScreen(service: service)));
    await tester.enterText(find.byType(TextField), '질문');
    await tester.pump();
    await tester.tap(find.byTooltip('메시지 전송'));
    service.pending.complete('응답');
    await tester.pumpAndSettle();
    await tester.tap(find.text('듣기'));
    await tester.pumpAndSettle();
    expect(find.text('합성한 목소리예요'), findsOneWidget);
    expect(find.textContaining('음성 재생을 제공하지 않습니다'), findsOneWidget);
    await tester.tap(find.text('확인'));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('음성 대화'));
    await tester.pumpAndSettle();
    expect(find.text('음성 화면 미리보기 · 마이크는 사용하지 않습니다.'), findsOneWidget);
    await tester.tap(find.byTooltip('텍스트 대화로 전환'));
    await tester.pumpAndSettle();
    expect(find.text('응답'), findsOneWidget);
  });

  testWidgets('empty input disabled, send waits then displays reply', (
    tester,
  ) async {
    final service = ControlledService();
    await tester.pumpWidget(MaterialApp(home: ChatScreen(service: service)));
    expect(find.textContaining('데모 모드'), findsOneWidget);
    expect(
      tester
          .widget<IconButton>(
            find.widgetWithIcon(IconButton, Icons.send_outlined),
          )
          .onPressed,
      isNull,
    );
    await tester.enterText(find.byType(TextField), '  안녕하세요  ');
    await tester.pump();
    await tester.tap(find.byTooltip('메시지 전송'));
    await tester.pump();
    expect(service.requests, ['안녕하세요']);
    expect(find.text('응답을 만드는 중'), findsOneWidget);
    expect(
      tester
          .widget<IconButton>(
            find.widgetWithIcon(IconButton, Icons.send_outlined),
          )
          .onPressed,
      isNull,
    );
    service.pending.complete('반가워요');
    await tester.pumpAndSettle();
    expect(find.text('반가워요'), findsOneWidget);
    expect(find.text('응답을 만드는 중'), findsNothing);
  });

  testWidgets('failure can retry without duplicating the user message', (
    tester,
  ) async {
    final service = ControlledService();
    await tester.pumpWidget(MaterialApp(home: ChatScreen(service: service)));
    await tester.enterText(find.byType(TextField), '안녕');
    await tester.pump();
    await tester.tap(find.byTooltip('메시지 전송'));
    service.pending.completeError(StateError('offline'));
    await tester.pumpAndSettle();
    expect(find.textContaining('응답을 받지 못했어요'), findsOneWidget);
    service.pending = Completer<String>();
    await tester.tap(find.text('다시 시도'));
    await tester.pump();
    service.pending.complete('다시 연결됐어요');
    await tester.pumpAndSettle();
    expect(service.requests, ['안녕', '안녕']);
    expect(find.text('안녕'), findsOneWidget);
    expect(find.text('다시 연결됐어요'), findsOneWidget);
  });

  testWidgets('failed message can be edited', (tester) async {
    final service = ControlledService();
    await tester.pumpWidget(MaterialApp(home: ChatScreen(service: service)));
    await tester.enterText(find.byType(TextField), '수정할 메시지');
    await tester.pump();
    await tester.tap(find.byTooltip('메시지 전송'));
    service.pending.completeError(StateError('offline'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('메시지 수정'));
    await tester.pumpAndSettle();
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      '수정할 메시지',
    );
    expect(find.textContaining('응답을 받지 못했어요'), findsNothing);
  });

  testWidgets('small screen and keyboard keep input and send visible', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 568);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetViewInsets);
    await tester.pumpWidget(const MaterialApp(home: ChatScreen(service: DemoChatService())));
    tester.view.viewInsets = const FakeViewPadding(bottom: 280);
    await tester.enterText(
      find.byType(TextField),
      List.filled(30, '긴 메시지').join('\n'),
    );
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(
      tester.getBottomRight(find.byType(TextField)).dy,
      lessThanOrEqualTo(288),
    );
    expect(
      tester.getBottomRight(find.byTooltip('메시지 전송')).dy,
      lessThanOrEqualTo(288),
    );
    await tester.tap(find.byTooltip('메시지 전송'));
    await tester.pumpAndSettle(const Duration(milliseconds: 800));
    expect(find.textContaining('화면 확인용 데모 응답'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('request can finish after screen is disposed', (tester) async {
    final service = ControlledService();
    await tester.pumpWidget(MaterialApp(home: ChatScreen(service: service)));
    await tester.enterText(find.byType(TextField), '안녕');
    await tester.pump();
    await tester.tap(find.byTooltip('메시지 전송'));
    await tester.pumpWidget(const SizedBox());
    service.pending.complete('응답');
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  });
}
