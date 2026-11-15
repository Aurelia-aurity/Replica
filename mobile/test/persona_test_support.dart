import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/profile/persona_models.dart';
import 'package:mobile/profile/persona_service.dart';

/// Preview account whose token header is a synthetic value.
class SyntheticAuth extends PreviewAuthService {
  @override
  Future<Map<String, String>> authorizationHeaders() async => {
    'Authorization': 'Bearer synthetic-token',
  };
}

http.Response jsonResponse(Object? value, [int status = 200]) => http.Response(
  jsonEncode(value),
  status,
  headers: {'content-type': 'application/json; charset=utf-8'},
);

Map<String, dynamic> personaJson(
  String id,
  String name, {
  String status = 'active',
}) => {
  'id': id,
  'name': name,
  'status': status,
  'data_revision': 1,
  'created_at': '2026-11-01T00:00:00Z',
  'updated_at': '2026-11-01T00:00:00Z',
};

/// Controllable [PersonaService]. Set an `*Error` to make the next calls fail,
/// or a `createGate` to hold a call until the test completes it.
class FakePersonaService implements PersonaService {
  FakePersonaService({List<Persona>? personas}) : personas = [...?personas];

  final List<Persona> personas;
  Object? listError, createError, updateError;
  Completer<void>? createGate;

  final created = <({String name, String id})>[];
  final updates = <({String id, PersonaUpdate update})>[];
  int listCalls = 0;

  @override
  Future<List<Persona>> list() async {
    listCalls++;
    if (listError != null) throw listError!;
    return [...personas];
  }

  @override
  Future<Persona> create(String name, {required String personaId}) async {
    created.add((name: name, id: personaId));
    await createGate?.future;
    if (createError != null) throw createError!;
    final persona = Persona(id: personaId, name: name);
    personas.insert(0, persona);
    return persona;
  }

  @override
  Future<Persona> update(String personaId, PersonaUpdate update) async {
    updates.add((id: personaId, update: update));
    if (updateError != null) throw updateError!;
    final index = personas.indexWhere((p) => p.id == personaId);
    return personas[index] = personas[index].applying(update);
  }
}

/// Names the first partner from the home card, as every new account has to do
/// before it can start a conversation.
Future<void> nameFirstPartner(
  WidgetTester tester, [
  String name = '합성 상대',
]) async {
  await tester.tap(find.text('프로필 만들기'));
  await tester.pumpAndSettle();
  await tester.enterText(find.byType(TextField), name);
  await tester.pump();
  await tester.tap(find.widgetWithText(FilledButton, '저장'));
  await tester.pumpAndSettle();
}
