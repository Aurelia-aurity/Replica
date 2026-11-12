import '../history/conversation.dart';
import '../services/api_client.dart';
import '../services/backend_service.dart';
import 'persona_models.dart';

/// Conversation partners (SC-02, SC-13). The app only sends the name; no
/// speech profile is requested or stored in this version.
abstract class PersonaService {
  /// Active partners, newest first (`GET /personas`).
  Future<List<Persona>> list();

  /// Creates a partner (`POST /personas`). [personaId] is chosen by the app and
  /// kept across retries so a lost response can never create a second partner.
  Future<Persona> create(String name, {required String personaId});
}

/// Data for the dummy account. Lives only for one login and is never sent
/// anywhere. It starts empty unless [personas] is given, so the dummy account
/// goes through the same name setting as a real one.
class MemoryPersonaService implements PersonaService {
  MemoryPersonaService({List<Persona>? personas, this.backend})
    : _personas = [...?personas];

  final BackendService? backend;
  final List<Persona> _personas;

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
}

/// Adapter for the app-backend contract (`GET /personas`, `POST /personas`,
/// `GET /personas/{id}`).
class HttpPersonaService implements PersonaService {
  HttpPersonaService(this._api);
  final ApiClient _api;

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
}

/// Id for a new partner; retries of the same save reuse it.
String newPersonaId() => newConversationId();
