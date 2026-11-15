import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/profile/persona_models.dart';
import 'package:mobile/profile/persona_service.dart';
import 'package:mobile/profile/style_profile_models.dart';
import 'package:mobile/profile/speech_profile_screen.dart';

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
  Object? listError, createError, updateError, profileError, analysisError;

  /// Answer of the profile lookup; [analysisResult] of an analysis request.
  StyleProfileState profileState = const StyleProfileState();
  ProfileAnalysis? analysisResult;
  final analysisRequests = <({String persona, String operation})>[];
  int profileCalls = 0;
  Completer<void>? createGate, updateGate, analysisGate;

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
    await updateGate?.future;
    if (updateError != null) throw updateError!;
    final index = personas.indexWhere((p) => p.id == personaId);
    return personas[index] = personas[index].applying(update);
  }

  @override
  Future<StyleProfileState> styleProfile(String personaId) async {
    profileCalls++;
    if (profileError != null) throw profileError!;
    return profileState;
  }

  @override
  Future<ProfileAnalysis> requestAnalysis(
    String personaId, {
    required String operationId,
  }) async {
    analysisRequests.add((persona: personaId, operation: operationId));
    await analysisGate?.future;
    if (analysisError != null) throw analysisError!;
    return analysisResult ?? analysisOf(AnalysisStatus.running, operationId);
  }
}

ProfileAnalysis analysisOf(
  AnalysisStatus status, [
  String operationId = 'op-1',
  String? errorCode,
]) => ProfileAnalysis(
  operationId: operationId,
  status: status,
  startedAt: DateTime.utc(2026, 11, 10),
  updatedAt: DateTime.utc(2026, 11, 10),
  errorCode: errorCode,
);

StyleProfile sampleProfile() => StyleProfile(
  version: 2,
  updatedAt: DateTime.utc(DateTime.now().year, 10, 6, 3),
  expressions: const ['밥은 먹었니', '조심히 다녀'],
  addressTerms: const ['우리 딸', '민지야'],
  endings: const ['~했니?', '~하렴'],
);

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
  // Saving a new partner moves on to SC-07; come back to the home card.
  if (find.byType(SpeechProfileScreen).evaluate().isNotEmpty) {
    await tester.pageBack();
    await tester.pumpAndSettle();
  }
}
