import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/record_upload_screen.dart';
import 'package:mobile/records/records_panel.dart';
import 'package:mobile/shared/conversation_widgets.dart';
import 'package:mobile/records/record_review_screen.dart';

import 'record_review_test.dart' show ReviewFixture;
import 'record_deletion_test.dart' show DeletionFixture;

void main() {
  const capture = bool.fromEnvironment('RECORD_UI_CAPTURES');
  setUpAll(() async {
    if (capture) {
      final bytes = await File('C:/Windows/Fonts/malgun.ttf').readAsBytes();
      await (FontLoader(
        'RecordPreview',
      )..addFont(Future.value(ByteData.sublistView(bytes)))).load();
      final icons = await File(
        'C:/flutter/bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf',
      ).readAsBytes();
      await (FontLoader(
        'MaterialIcons',
      )..addFont(Future.value(ByteData.sublistView(icons)))).load();
    }
  });

  Future<void> show(WidgetTester tester, Widget child, GlobalKey key) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(390, 844);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    await tester.pumpWidget(
      RepaintBoundary(
        key: key,
        child: MaterialApp(
          debugShowCheckedModeBanner: false,
          theme: ThemeData(
            useMaterial3: true,
            fontFamily: capture ? 'RecordPreview' : null,
            colorScheme: ColorScheme.fromSeed(seedColor: replicaGreen),
            scaffoldBackgroundColor: replicaBackground,
            filledButtonTheme: FilledButtonThemeData(
              style: FilledButton.styleFrom(
                minimumSize: const Size(0, 48),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(14),
                ),
              ),
            ),
          ),
          home: child,
        ),
      ),
    );
    await tester.pumpAndSettle();
  }

  Future<void> save(WidgetTester tester, GlobalKey key, String name) async {
    if (!capture) return;
    final boundary =
        key.currentContext!.findRenderObject()! as RenderRepaintBoundary;
    await tester.runAsync(() async {
      final image = await boundary.toImage(pixelRatio: 2);
      final data = await image.toByteData(format: ui.ImageByteFormat.png);
      final output = File('docs/screenshots/issue9/$name.png');
      await output.parent.create(recursive: true);
      await output.writeAsBytes(data!.buffer.asUint8List());
      image.dispose();
    });
  }

  testWidgets(
    'SC03 puts status beside filename and retains filters and notice',
    (tester) async {
      final c = RecordController(PreviewRecordService());
      addTearDown(c.dispose);
      c.entries = [
        RecordEntry(
          id: '1',
          name: '합성 대화.txt',
          kind: RecordKind.text,
          status: RecordStatus.ready,
          confirmed: true,
          createdAt: DateTime(2026, 9, 20),
        ),
        RecordEntry(
          id: '2',
          name: '합성 일기.txt',
          kind: RecordKind.text,
          status: RecordStatus.ready,
          confirmed: true,
          createdAt: DateTime(2026, 9, 20),
        ),
        RecordEntry(
          id: '3',
          name: '합성 음성메모.m4a',
          kind: RecordKind.audio,
          status: RecordStatus.ready,
          durationSeconds: 192,
          createdAt: DateTime(2026, 10, 6),
        ),
        RecordEntry(
          id: '4',
          name: '합성 녹음.wav',
          kind: RecordKind.audio,
          status: RecordStatus.processing,
          durationSeconds: 48,
          createdAt: DateTime(2026, 10, 7),
        ),
        RecordEntry(
          id: '5',
          name: '합성 통화.mp3',
          kind: RecordKind.audio,
          status: RecordStatus.failed,
          createdAt: DateTime(2026, 10, 7),
        ),
      ];
      final key = GlobalKey();
      await show(
        tester,
        Scaffold(
          body: RecordsPanel(controller: c),
          bottomNavigationBar: NavigationBar(
            selectedIndex: 1,
            destinations: const [
              NavigationDestination(
                icon: Icon(Icons.home_outlined),
                label: '홈',
              ),
              NavigationDestination(
                icon: Icon(Icons.folder_outlined),
                label: '기록',
              ),
              NavigationDestination(
                icon: Icon(Icons.chat_bubble_outline),
                label: '대화',
              ),
              NavigationDestination(icon: Icon(Icons.tune), label: '설정'),
            ],
          ),
        ),
        key,
      );
      expect(find.text('전체 5'), findsOneWidget);
      expect(
        tester.getTopLeft(find.text('확인 필요')).dx,
        greaterThan(tester.getTopLeft(find.text('합성 음성메모.m4a')).dx),
      );
      expect(find.text('전사 중'), findsOneWidget);
      expect(find.text('확정된 기록만 말투 프로필과 대화에 쓰여요.'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await save(tester, key, 'record-list');
    },
  );

  for (final confirmed in [false, true]) {
    testWidgets('SC05 and SC06 review layout confirmed=$confirmed', (
      tester,
    ) async {
      final service = ReviewFixture()..confirmed = confirmed;
      service.body = '밥은 먹었나? 요즘 날이 추워서 걱정이다.\n주말에 내려오면 김치 좀 싸 줄게.\n아빠가 너 보고 싶다 카더라.\n감기 조심하고 따뜻하게 챙겨 입어라.';
      final records = RecordController(service);
      addTearDown(records.dispose);
      final key = GlobalKey();
      await show(
        tester,
        RecordReviewScreen(records: records, entry: service.entry),
        key,
      );
      expect(find.text(confirmed ? '기록 상세' : '전사 확인'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await save(
        tester,
        key,
        confirmed ? 'record-detail' : 'transcript-review',
      );
    });
  }

  testWidgets('SC06 deletion confirmation and partial failure', (tester) async {
    final service = DeletionFixture()..confirmed = true;
    final records = RecordController(service);
    addTearDown(records.dispose);
    final key = GlobalKey();
    await show(
      tester,
      RecordReviewScreen(records: records, entry: service.entry),
      key,
    );
    await tester.tap(find.byTooltip('기록 삭제'));
    await tester.pumpAndSettle();
    expect(find.text('이 기록을 삭제할까요?'), findsOneWidget);
    await save(tester, key, 'record-delete-confirm');
    await tester.tap(find.byKey(const ValueKey('delete-confirm')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('상태 다시 확인'));
    await tester.pumpAndSettle();
    expect(find.text('남은 항목: 검색 색인'), findsOneWidget);
    expect(tester.getBottomRight(find.byKey(const ValueKey('delete-confirm'))).dy, lessThanOrEqualTo(844));
    await save(tester, key, 'record-delete-partial-failure');
    expect(tester.takeException(), isNull);
  });

  testWidgets('SC04 kind cards clear file consent and upload stays fixed', (
    tester,
  ) async {
    final c = RecordController(PreviewRecordService());
    addTearDown(c.dispose);
    c.select(
      RecordFile(
        name: '합성 대화.txt',
        sizeBytes: 248 * 1024,
        openRead: () => const Stream.empty(),
      ),
    );
    c.setConsent(true);
    final key = GlobalKey();
    await show(
      tester,
      RecordUploadScreen(controller: c, picker: () async => null),
      key,
    );
    expect(find.text('어떤 기록인가요?'), findsOneWidget);
    expect(find.text('변경'), findsOneWidget);
    final upload = find.byKey(const ValueKey('record-upload'));
    final position = tester.getTopLeft(upload);
    await save(tester, key, 'record-upload');
    await tester.drag(find.byType(ListView), const Offset(0, -200));
    await tester.pumpAndSettle();
    expect(tester.getTopLeft(upload), position);
    await tester.drag(find.byType(ListView), const Offset(0, 500));
    await tester.pumpAndSettle();
    await tester.tap(find.text('음성'));
    await tester.pumpAndSettle();
    expect(c.selected, isNull);
    expect(c.consent, isFalse);
    expect(tester.widget<FilledButton>(upload).onPressed, isNull);
    expect(tester.takeException(), isNull);
  });
}
