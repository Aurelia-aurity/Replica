import 'dart:async';
import 'dart:convert';

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

/// Synthetic profile. Values are examples, not a user's records.
StyleProfile sampleProfile({
  int version = 1,
  DateTime? updatedAt,
  int? count = 11,
  List<String> expressions = const ['밥은 먹었나', '걱정이다'],
  List<String> addressTerms = const ['우리 딸', '니'],
  List<String> endings = const ['~나?', '~데이'],
}) => StyleProfile(
  version: version,
  updatedAt: updatedAt ?? DateTime.now(),
  expressions: expressions,
  addressTerms: addressTerms,
  endings: endings,
  evidenceRecordCount: count,
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

Map<String, dynamic> profileJson({
  String status = 'ready',
  Object? content,
  int version = 2,
}) => {
  'id': 'profile-1',
  'persona_id': 'p1',
  'version': version,
  'source_revision': 3,
  'status': status,
  'model_name': 'synthetic',
  'created_at': '2026-10-06T00:00:00Z',
  'updated_at': '2026-10-07T00:00:00Z',
  'content':
      content ??
      {
        'schema_version': 1,
        'speech_style': {
          'tone': ['다정한 어조'],
          'address_terms': ['너'],
          'sentence_endings': ['~니?', '~지.'],
          'frequent_expressions': ['밥은 챙겨 먹었니?'],
        },
        'conversation_habits': <String>[],
        'limitations': <String>[],
      },
};

const allFeatures = {
  PersonaFeature.rename,
  PersonaFeature.regenerate,
  PersonaFeature.excludeItem,
};

/// Controllable [PersonaService]. Set an `*Error` to make the next calls fail,
/// or a `*Gate` to hold a call until the test completes it.
class FakePersonaService implements PersonaService {
  FakePersonaService({
    List<Persona>? personas,
    this.profileValue,
    this.supported = allFeatures,
    this.preview = false,
  }) : personas = [...?personas];

  final List<Persona> personas;
  StyleProfile? profileValue;
  final Set<PersonaFeature> supported;
  final bool preview;

  Object? listError, profileError, createError, renameError, regenerateError;
  Completer<void>? profileGate, createGate, regenerateGate;
  StyleProfile? regenerated;

  final created = <({String name, String id})>[];
  final renames = <String>[];
  int listCalls = 0, profileCalls = 0, regenerateCalls = 0;

  @override
  bool get isPreview => preview;
  @override
  Set<PersonaFeature> get supportedFeatures => supported;

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
  Future<Persona> rename(String personaId, String name) async {
    renames.add(name);
    if (renameError != null) throw renameError!;
    final index = personas.indexWhere((p) => p.id == personaId);
    return personas[index] = personas[index].renamed(name);
  }

  @override
  Future<StyleProfile?> profile(String personaId) async {
    profileCalls++;
    // The answer is fixed when the request is made, even if it arrives later.
    final answer = profileValue;
    await profileGate?.future;
    if (profileError != null) throw profileError!;
    return answer;
  }

  @override
  Future<StyleProfile> regenerate(String personaId) async {
    regenerateCalls++;
    await regenerateGate?.future;
    if (regenerateError != null) throw regenerateError!;
    return profileValue =
        regenerated ??
        sampleProfile(
          version: (profileValue?.version ?? 0) + 1,
          expressions: const ['새 표현'],
        );
  }

  @override
  Future<StyleProfile> excludeItem(
    String personaId,
    ProfileSection section,
    String value,
  ) async => profileValue!;
}
