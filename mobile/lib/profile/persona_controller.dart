import 'package:flutter/foundation.dart';

import 'persona_models.dart';
import 'persona_service.dart';

enum LoadStatus { idle, loading, ready, failed }

/// State shared by the home card and SC-13 for one login.
///
/// The current partner is the one picked on the home card, or the first active
/// one the server lists when nothing was picked. New conversations are created
/// for it.
class PersonaController extends ChangeNotifier {
  PersonaController(this.service);
  final PersonaService service;

  LoadStatus _status = LoadStatus.idle;
  List<Persona> _personas = const [];
  String? _selectedId;
  int _request = 0;
  bool _disposed = false;

  LoadStatus get status => _status;
  List<Persona> get personas => _personas;
  Persona? get current {
    for (final persona in _personas) {
      if (persona.id == _selectedId) return persona;
    }
    return _personas.isEmpty ? null : _personas.first;
  }

  /// Picks the partner new conversations are created for. Ignored for an id
  /// that is not in the list.
  void select(String id) {
    if (id == current?.id || !_personas.any((p) => p.id == id)) return;
    _selectedId = id;
    _notify();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  /// Loads the partners. A late answer to an older request is dropped.
  Future<void> load() async {
    final request = ++_request;
    _status = LoadStatus.loading;
    _notify();
    try {
      final items = await service.list();
      if (_disposed || request != _request) return;
      _personas = items;
      _status = LoadStatus.ready;
    } catch (_) {
      if (_disposed || request != _request) return;
      _status = LoadStatus.failed;
    }
    _notify();
  }

  /// Creates a partner (SC-13). Throws on failure and changes nothing.
  Future<Persona> create(String name, {required String personaId}) async {
    final persona = await service.create(name, personaId: personaId);
    if (!_disposed) {
      _personas = [persona, ..._personas.where((p) => p.id != persona.id)];
      _selectedId = persona.id;
      _status = LoadStatus.ready;
      _request++; // a list answer made before this must not drop the partner
      _notify();
    }
    return persona;
  }
}
