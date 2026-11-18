import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/profile/persona_controller.dart';
import 'package:mobile/profile/persona_models.dart';
import 'package:mobile/profile/persona_name_screen.dart';
import 'package:mobile/services/backend_service.dart';

import 'persona_test_support.dart';

class Opened {
  Persona? result;
  bool returned = false;
}

/// Opens SC-13 from a plain page so cancel and back have somewhere to return.
Future<Opened> openName(
  WidgetTester tester,
  PersonaController controller, {
  Persona? editing,
}) async {
  final opened = Opened();
  await tester.pumpWidget(
    MaterialApp(
      home: Builder(
        builder: (context) => Scaffold(
          body: Center(
            child: TextButton(
              onPressed: () async {
                opened.result = await Navigator.of(context).push<Persona>(
                  MaterialPageRoute(
                    builder: (_) => PersonaNameScreen(
                      controller: controller,
                      editing: editing,
                    ),
                  ),
                );
                opened.returned = true;
              },
              child: const Text('열기'),
            ),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.text('열기'));
  await tester.pumpAndSettle();
  return opened;
}

PersonaController newController(FakePersonaService service) {
  final controller = PersonaController(service);
  addTearDown(controller.dispose);
  return controller;
}

Finder get saveButton => find.widgetWithText(FilledButton, '저장');
bool saveEnabled(WidgetTester tester) =>
    tester.widget<FilledButton>(saveButton).onPressed != null;

void main() {
  testWidgets('shows title, guidance and example; save starts disabled', (
    tester,
  ) async {
    await openName(tester, newController(FakePersonaService()));
    expect(find.text('프로필 만들기'), findsOneWidget);
    expect(find.text('누구와\n대화하고 싶으세요?'), findsOneWidget);
    expect(find.textContaining('바꿀 수 없어요'), findsNothing);
    expect(find.textContaining('말투'), findsNothing);
    expect(find.textContaining('학습'), findsNothing);
    expect(find.text('예: 엄마, 할머니, 민지'), findsOneWidget);
    expect(find.text('0 / 50'), findsOneWidget);
    expect(saveEnabled(tester), false);
  });

  testWidgets('create mode starts empty, without the edit title', (
    tester,
  ) async {
    await openName(tester, newController(FakePersonaService()));
    expect(find.text('대화 상대 이름 수정'), findsNothing);
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      isEmpty,
    );
  });

  group('edit mode', () {
    const original = Persona(
      id: 'p1',
      name: '김영희',
      displayName: '엄마',
      providerAddress: '우리 딸',
    );

    Future<(FakePersonaService, PersonaController, Opened)> openEdit(
      WidgetTester tester, {
      Persona persona = original,
    }) async {
      final service = FakePersonaService(personas: [persona]);
      final controller = newController(service);
      await controller.load();
      final opened = await openName(
        tester,
        controller,
        editing: controller.current,
      );
      return (service, controller, opened);
    }

    String fieldText(WidgetTester tester) =>
        tester.widget<TextField>(find.byType(TextField)).controller!.text;

    testWidgets('prefills the shown name with the edit title and guidance', (
      tester,
    ) async {
      await openEdit(tester);
      expect(find.text('대화 상대 이름 수정'), findsOneWidget);
      expect(find.text('프로필 만들기'), findsNothing);
      expect(find.text('이름이나 호칭을\n바꿔 볼까요?'), findsOneWidget);
      expect(find.textContaining('올린 기록은 그대로예요'), findsOneWidget);
      expect(fieldText(tester), '엄마');
      expect(find.text('2 / 50'), findsOneWidget);
      expect(saveEnabled(tester), true);
    });

    testWidgets('without a display name the creation name is prefilled', (
      tester,
    ) async {
      await openEdit(
        tester,
        persona: const Persona(id: 'p1', name: '김영희'),
      );
      expect(fieldText(tester), '김영희');
    });

    testWidgets('saving sends only the display name and returns the partner', (
      tester,
    ) async {
      final (service, controller, opened) = await openEdit(tester);
      await tester.enterText(find.byType(TextField), ' 어머니 ');
      await tester.pump();
      await tester.tap(saveButton);
      await tester.pumpAndSettle();
      expect(service.created, isEmpty);
      expect(service.updates, hasLength(1));
      expect(service.updates.single.id, 'p1');
      expect(service.updates.single.update.toJson(), {'display_name': '어머니'});
      expect(opened.result!.label, '어머니');
      final current = controller.current!;
      expect(current.label, '어머니');
      expect(current.name, '김영희');
      expect(current.providerAddress, '우리 딸');
      expect(find.byType(PersonaNameScreen), findsNothing);
    });

    testWidgets('saving an unchanged name sends nothing', (tester) async {
      final (service, _, opened) = await openEdit(tester);
      await tester.enterText(find.byType(TextField), ' 엄마 ');
      await tester.pump();
      await tester.tap(saveButton);
      await tester.pumpAndSettle();
      expect(service.updates, isEmpty);
      expect(opened.result, original);
    });

    testWidgets('a blank name cannot be saved', (tester) async {
      final (service, _, _) = await openEdit(tester);
      await tester.enterText(find.byType(TextField), '   ');
      await tester.pump();
      expect(saveEnabled(tester), false);
      expect(service.updates, isEmpty);
    });

    testWidgets('cancel and back keep the existing name', (tester) async {
      final (service, controller, opened) = await openEdit(tester);
      await tester.enterText(find.byType(TextField), '다른 이름');
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(opened.returned, true);
      expect(opened.result, isNull);

      await tester.tap(find.text('열기'));
      await tester.pumpAndSettle();
      expect(fieldText(tester), '엄마');
      await tester.enterText(find.byType(TextField), '또 다른 이름');
      await tester.pageBack();
      await tester.pumpAndSettle();
      expect(opened.result, isNull);
      expect(service.updates, isEmpty);
      expect(controller.current!.label, '엄마');
    });

    testWidgets('failure keeps the typed name and a retry sends it again', (
      tester,
    ) async {
      final (service, controller, opened) = await openEdit(tester);
      service.updateError = StateError('offline');
      await tester.enterText(find.byType(TextField), '어머니');
      await tester.pump();
      await tester.tap(saveButton);
      await tester.pumpAndSettle();
      expect(find.textContaining('저장하지 못했어요'), findsOneWidget);
      expect(fieldText(tester), '어머니');
      expect(controller.current!.label, '엄마');
      expect(opened.returned, false);

      service.updateError = null;
      await tester.tap(find.text('다시 시도'));
      await tester.pumpAndSettle();
      expect(service.updates, hasLength(2));
      expect(opened.result!.label, '어머니');
      expect(controller.current!.label, '어머니');
    });

    testWidgets('a server without the rename API says it is not ready', (
      tester,
    ) async {
      final (service, controller, opened) = await openEdit(tester);
      service.updateError = const BackendFailure(
        BackendFailureKind.generic,
        code: apiNotProvided,
      );
      await tester.enterText(find.byType(TextField), '어머니');
      await tester.pump();
      await tester.tap(saveButton);
      await tester.pumpAndSettle();
      expect(find.textContaining('이름 수정은 아직 준비 중이에요'), findsOneWidget);
      expect(find.text('다시 시도'), findsNothing);
      expect(fieldText(tester), '어머니');
      expect(controller.current!.label, '엄마');
      expect(opened.returned, false);
    });

    testWidgets('rejected input explains itself', (tester) async {
      final (service, _, _) = await openEdit(tester);
      service.updateError = const BackendFailure(
        BackendFailureKind.generic,
        code: 'INVALID_INPUT',
      );
      await tester.enterText(find.byType(TextField), '어머니');
      await tester.pump();
      await tester.tap(saveButton);
      await tester.pumpAndSettle();
      expect(find.text('입력한 내용을 확인해 주세요.'), findsOneWidget);
    });

    testWidgets('saving blocks duplicate requests and leaving the screen', (
      tester,
    ) async {
      final gate = Completer<void>();
      final (service, _, opened) = await openEdit(tester);
      service.updateGate = gate;
      await tester.enterText(find.byType(TextField), '어머니');
      await tester.pump();
      await tester.tap(saveButton);
      await tester.pump();
      await tester.tap(find.byType(FilledButton), warnIfMissed: false);
      await tester.pump();
      expect(find.bySemanticsLabel('저장 중'), findsOneWidget);
      await tester.pageBack();
      await tester.pump();
      expect(find.byType(PersonaNameScreen), findsOneWidget);
      expect(service.updates, hasLength(1));
      gate.complete();
      await tester.pumpAndSettle();
      expect(opened.result!.label, '어머니');
    });
  });

  testWidgets('blank and whitespace-only input cannot be saved', (
    tester,
  ) async {
    final service = FakePersonaService();
    await openName(tester, newController(service));
    await tester.enterText(find.byType(TextField), '    ');
    await tester.pump();
    expect(saveEnabled(tester), false);
    await tester.tap(saveButton, warnIfMissed: false);
    await tester.pump();
    expect(service.created, isEmpty);
    await tester.enterText(find.byType(TextField), '엄마');
    await tester.pump();
    expect(saveEnabled(tester), true);
  });

  testWidgets('name is trimmed, saved, and returned to the caller', (
    tester,
  ) async {
    final service = FakePersonaService();
    final controller = newController(service);
    final opened = await openName(tester, controller);
    await tester.enterText(find.byType(TextField), '  엄마  ');
    await tester.pump();
    expect(find.text('6 / 50'), findsOneWidget);
    await tester.tap(saveButton);
    await tester.pumpAndSettle();
    expect(service.created.single.name, '엄마');
    expect(opened.returned, true);
    expect(opened.result!.name, '엄마');
    expect(controller.current!.name, '엄마');
    expect(find.byType(PersonaNameScreen), findsNothing);
  });

  testWidgets('input is limited to 50 characters', (tester) async {
    final service = FakePersonaService();
    await openName(tester, newController(service));
    await tester.enterText(find.byType(TextField), '가' * 60);
    await tester.pump();
    expect(find.text('50 / 50'), findsOneWidget);
    await tester.tap(saveButton);
    await tester.pumpAndSettle();
    expect(service.created.single.name, '가' * 50);
  });

  testWidgets('cancel and back return without saving anything', (tester) async {
    final service = FakePersonaService();
    final controller = newController(service);
    final opened = await openName(tester, controller);
    await tester.enterText(find.byType(TextField), '엄마');
    await tester.tap(find.text('취소'));
    await tester.pumpAndSettle();
    expect(opened.returned, true);
    expect(opened.result, isNull);
    expect(service.created, isEmpty);
    expect(controller.current, isNull);

    await tester.tap(find.text('열기'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), '할머니');
    await tester.pageBack();
    await tester.pumpAndSettle();
    expect(find.byType(PersonaNameScreen), findsNothing);
    expect(service.created, isEmpty);
  });

  testWidgets('saving blocks duplicate requests and leaving the screen', (
    tester,
  ) async {
    final service = FakePersonaService()..createGate = Completer<void>();
    final opened = await openName(tester, newController(service));
    await tester.enterText(find.byType(TextField), '엄마');
    await tester.pump();
    await tester.tap(saveButton);
    await tester.pump();
    await tester.tap(find.byType(FilledButton), warnIfMissed: false);
    await tester.pump();
    expect(service.created, hasLength(1));
    expect(find.bySemanticsLabel('저장 중'), findsOneWidget);
    expect(
      tester
          .widget<OutlinedButton>(find.widgetWithText(OutlinedButton, '취소'))
          .onPressed,
      isNull,
    );
    await tester.pageBack();
    await tester.pump();
    expect(find.byType(PersonaNameScreen), findsOneWidget);
    service.createGate!.complete();
    await tester.pumpAndSettle();
    expect(opened.result!.name, '엄마');
  });

  testWidgets('failure keeps the input and a retry reuses the same id', (
    tester,
  ) async {
    final service = FakePersonaService()..createError = StateError('offline');
    final controller = newController(service);
    final opened = await openName(tester, controller);
    await tester.enterText(find.byType(TextField), '엄마');
    await tester.pump();
    await tester.tap(saveButton);
    await tester.pumpAndSettle();
    expect(find.textContaining('저장하지 못했어요'), findsOneWidget);
    expect(find.byType(PersonaNameScreen), findsOneWidget);
    expect(opened.returned, false);
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      '엄마',
    );
    expect(controller.current, isNull);

    service.createError = null;
    await tester.tap(find.text('다시 시도'));
    await tester.pumpAndSettle();
    expect(service.created, hasLength(2));
    expect(service.created.first.id, service.created.last.id);
    expect(opened.result!.name, '엄마');
  });

  testWidgets('a different name after a failure is a new request', (
    tester,
  ) async {
    final service = FakePersonaService()..createError = StateError('offline');
    await openName(tester, newController(service));
    await tester.enterText(find.byType(TextField), '엄마');
    await tester.pump();
    await tester.tap(saveButton);
    await tester.pumpAndSettle();
    service.createError = null;
    await tester.enterText(find.byType(TextField), '엄마 2');
    await tester.pump();
    await tester.tap(saveButton);
    await tester.pumpAndSettle();
    expect(service.created.first.id, isNot(service.created.last.id));
  });

  testWidgets('the example stays visible under the field while typing', (
    tester,
  ) async {
    await openName(tester, newController(FakePersonaService()));
    await tester.enterText(find.byType(TextField), '엄마');
    await tester.pump();
    expect(find.text('예: 엄마, 할머니, 민지'), findsOneWidget);
    expect(find.text('2 / 50'), findsOneWidget);
    // The label is its own text above the field, not a floating label.
    final label = find.text('대화 상대 이름 또는 호칭');
    expect(label, findsOneWidget);
    expect(
      tester.getBottomLeft(label).dy,
      lessThan(tester.getTopLeft(find.byType(TextField)).dy),
    );
  });

  testWidgets('cancel and save keep the 1 : 1.6 layout, 54 high', (
    tester,
  ) async {
    await openName(tester, newController(FakePersonaService()));
    final cancel = tester.getSize(find.widgetWithText(OutlinedButton, '취소'));
    final save = tester.getSize(saveButton);
    expect(cancel.height, 54);
    expect(save.height, 54);
    expect(save.width / cancel.width, closeTo(1.6, 0.05));
  });

  for (final inset in [0.0, 34.0]) {
    testWidgets('the failure notice clears the buttons (bottom inset $inset)', (
      tester,
    ) async {
      tester.view.viewPadding = FakeViewPadding(bottom: inset);
      tester.view.padding = FakeViewPadding(bottom: inset);
      addTearDown(tester.view.resetViewPadding);
      addTearDown(tester.view.resetPadding);
      final service = FakePersonaService()..createError = StateError('offline');
      await openName(tester, newController(service));
      await tester.enterText(find.byType(TextField), '엄마');
      await tester.pump();
      await tester.tap(saveButton);
      await tester.pumpAndSettle();
      // The SnackBar widget includes its margin; measure the visible body.
      final body = tester.getRect(
        find
            .descendant(
              of: find.byType(SnackBar),
              matching: find.byType(Material),
            )
            .first,
      );
      expect(body.height, lessThan(120)); // it really is the notice body
      expect(
        body.bottom,
        lessThanOrEqualTo(tester.getRect(saveButton).top),
        reason: 'the notice must not cover the save button',
      );
    });
  }

  testWidgets('rejected input explains itself', (tester) async {
    final service = FakePersonaService()
      ..createError = const BackendFailure(
        BackendFailureKind.generic,
        code: 'INVALID_INPUT',
      );
    await openName(tester, newController(service));
    await tester.enterText(find.byType(TextField), '엄마');
    await tester.pump();
    await tester.tap(saveButton);
    await tester.pumpAndSettle();
    expect(find.text('입력한 내용을 확인해 주세요.'), findsOneWidget);
  });

  testWidgets('small screen with keyboard keeps field and buttons usable', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 568);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetViewInsets);
    await openName(tester, newController(FakePersonaService()));
    tester.view.viewInsets = const FakeViewPadding(bottom: 280);
    await tester.enterText(find.byType(TextField), '아주아주아주긴 이름 호칭 입력 확인용 ' * 3);
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(tester.getBottomRight(saveButton).dy, lessThanOrEqualTo(288));
    expect(tester.getBottomRight(find.byType(TextField)).dy, lessThan(288));
  });

  test('name rules count characters, not bytes', () {
    expect(normalizePersonaName('  엄마 '), '엄마');
    expect(normalizePersonaName(' \n\t '), isNull);
    expect(normalizePersonaName(''), isNull);
    expect(normalizePersonaName('가' * 50), '가' * 50);
    expect(normalizePersonaName('가' * 51), isNull);
    expect(normalizePersonaName('👩‍👧' * 50), isNotNull);
    expect(normalizePersonaName('👩‍👧' * 51), isNull);
  });
}
