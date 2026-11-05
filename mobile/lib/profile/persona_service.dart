import '../history/conversation.dart';
import '../services/api_client.dart';
import '../services/backend_service.dart';
import 'persona_models.dart';

/// Conversation partners and their speech profile (SC-02, SC-07, SC-13).
///
/// Only operations the app-backend contract provides are connected to the
/// server. For the others [supportedFeatures] is empty and the call throws
/// [PersonaFeatureUnavailable]; nothing is faked on the app side.
abstract class PersonaService {
  /// True when every value comes from built-in preview data, never a server.
  bool get isPreview;

  /// Optional operations the connected API can actually perform.
  Set<PersonaFeature> get supportedFeatures;

  /// Active partners, newest first (`GET /personas`).
  Future<List<Persona>> list();

  /// Creates a partner (`POST /personas`). [personaId] is chosen by the app and
  /// kept across retries so a lost response can never create a second partner.
  Future<Persona> create(String name, {required String personaId});

  /// Changes only the name. The profile items are kept.
  Future<Persona> rename(String personaId, String name);

  /// The current `ready` profile, or null when the server has none. A null
  /// answer does not say whether it is missing, being built or outdated.
  Future<StyleProfile?> profile(String personaId);

  /// Builds the profile again from all confirmed records.
  Future<StyleProfile> regenerate(String personaId);

  /// Removes one extracted item. Proposed in the screen plan, not adopted yet.
  Future<StyleProfile> excludeItem(
    String personaId,
    ProfileSection section,
    String value,
  );
}

/// Preview data for the dummy account. Lives only for one login and is never
/// sent anywhere; screens label it as preview data.
class MemoryPersonaService implements PersonaService {
  MemoryPersonaService({
    List<Persona>? personas,
    Map<String, StyleProfile>? profiles,
    this.regenerateDelay = const Duration(milliseconds: 1200),
    this.backend,
  }) : _personas = [...?personas],
       _profiles = {...?profiles};

  /// Sample from the screen plan (SC-07). Its id matches the preview chat.
  factory MemoryPersonaService.preview({BackendService? backend}) =>
      MemoryPersonaService(
        backend: backend,
        personas: const [Persona(id: 'preview', name: '엄마')],
        profiles: {
          'preview': StyleProfile(
            version: 1,
            updatedAt: DateTime(2026, 10, 6),
            expressions: const ['밥은 먹었나', '걱정이다', '단디 해라', '그래 알았다'],
            addressTerms: const ['우리 딸', '니'],
            endings: const ['~나?', '~데이', '~카더라', '~아이가'],
            evidenceRecordCount: 11,
          ),
        },
      );

  final BackendService? backend;
  final Duration regenerateDelay;
  final List<Persona> _personas;
  final Map<String, StyleProfile> _profiles;

  @override
  bool get isPreview => true;
  @override
  Set<PersonaFeature> get supportedFeatures => PersonaFeature.values.toSet();

  Future<T> _run<T>(Future<T> Function() action) =>
      backend == null ? action() : backend!.run(action);

  @override
  Future<List<Persona>> list() => _run(() async => [..._personas]);

  @override
  Future<Persona> create(String name, {required String personaId}) =>
      _run(() async {
        for (final persona in _personas) {
          if (persona.id != personaId) continue;
          if (persona.name == name) return persona;
          throw StateError('Persona id already used');
        }
        final persona = Persona(id: personaId, name: name);
        _personas.insert(0, persona);
        return persona;
      });

  int _indexOf(String id) {
    final index = _personas.indexWhere((persona) => persona.id == id);
    if (index < 0) throw StateError('Unknown persona');
    return index;
  }

  @override
  Future<Persona> rename(String personaId, String name) => _run(() async {
    final index = _indexOf(personaId);
    return _personas[index] = _personas[index].renamed(name);
  });

  @override
  Future<StyleProfile?> profile(String personaId) =>
      _run(() async => _profiles[personaId]);

  @override
  Future<StyleProfile> regenerate(String personaId) => _run(() async {
    final current = _profiles[personaId];
    await Future<void>.delayed(regenerateDelay);
    if (current == null) throw StateError('No confirmed records');
    return _profiles[personaId] = current.copyWith(
      version: current.version + 1,
      updatedAt: DateTime.now(),
    );
  });

  @override
  Future<StyleProfile> excludeItem(
    String personaId,
    ProfileSection section,
    String value,
  ) => _run(() async {
    final current = _profiles[personaId];
    if (current == null) throw StateError('No profile');
    List<String> without(ProfileSection target) => [
      for (final item in current.itemsOf(target))
        if (target != section || item != value) item,
    ];
    return _profiles[personaId] = current.copyWith(
      expressions: without(ProfileSection.expressions),
      addressTerms: without(ProfileSection.addressTerms),
      endings: without(ProfileSection.endings),
    );
  });
}

/// Adapter for the app-backend contract. `GET /personas/{id}/style-profile` is
/// still marked as proposed there and the backend has no route for it yet, so a
/// 404 is reported as a failed lookup rather than as "no profile".
class HttpPersonaService implements PersonaService {
  HttpPersonaService(this._api);
  final ApiClient _api;

  @override
  bool get isPreview => false;

  /// The contract has no rename, regenerate or exclusion endpoint.
  @override
  Set<PersonaFeature> get supportedFeatures => const {};

  @override
  Future<List<Persona>> list() async {
    final result = <Persona>[];
    var offset = 0;
    while (true) {
      final json = await _api.request(
        'GET',
        '/personas',
        query: {'limit': '100', 'offset': '$offset'},
      ) as Map<String, dynamic>;
      final page = (json['items'] as List).cast<Map<String, dynamic>>();
      final limit = json['limit'] as int;
      if (limit < 1) throw const FormatException('Invalid limit');
      result.addAll(
        page.where((item) => item['status'] == 'active').map(Persona.fromJson),
      );
      if (page.length < limit) return result;
      offset += page.length;
    }
  }

  @override
  Future<Persona> create(String name, {required String personaId}) async {
    try {
      final json = await _api.request(
        'POST',
        '/personas',
        body: {'name': name, 'persona_id': personaId},
      );
      return Persona.fromJson(json as Map<String, dynamic>);
    } catch (error) {
      if (_isDefiniteRejection(error)) rethrow;
      // The write may have succeeded before the answer was lost (timeout,
      // dropped connection, 409 on a retry). Look the id up before reporting
      // failure so retrying never leaves a second partner behind.
      final existing = await _find(personaId);
      if (existing != null && existing.name == name) return existing;
      rethrow;
    }
  }

  bool _isDefiniteRejection(Object error) =>
      error is BackendFailure &&
      (error.kind == BackendFailureKind.unauthenticated ||
          error.kind == BackendFailureKind.forbidden ||
          error.kind == BackendFailureKind.staleSession ||
          error.code == 'INVALID_INPUT');

  Future<Persona?> _find(String id) async {
    try {
      final json = await _api.request(
        'GET',
        '/personas/${Uri.encodeComponent(id)}',
      );
      return Persona.fromJson(json as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  @override
  Future<StyleProfile?> profile(String personaId) async {
    final json = await _api.request(
      'GET',
      '/personas/${Uri.encodeComponent(personaId)}/style-profile',
    );
    if (json == null) return null;
    return StyleProfile.fromJson(json as Map<String, dynamic>);
  }

  @override
  Future<Persona> rename(String personaId, String name) =>
      Future.error(const PersonaFeatureUnavailable(PersonaFeature.rename));

  @override
  Future<StyleProfile> regenerate(String personaId) =>
      Future.error(const PersonaFeatureUnavailable(PersonaFeature.regenerate));

  @override
  Future<StyleProfile> excludeItem(
    String personaId,
    ProfileSection section,
    String value,
  ) =>
      Future.error(const PersonaFeatureUnavailable(PersonaFeature.excludeItem));
}

/// Id for a new partner; retries of the same save reuse it.
String newPersonaId() => newConversationId();
