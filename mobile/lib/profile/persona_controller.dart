import 'package:flutter/foundation.dart';

import 'persona_models.dart';
import 'persona_service.dart';

enum LoadStatus { idle, loading, ready, failed }

enum RegenerationStatus { idle, running, succeeded, failed }

/// State shared by the home card, SC-07 and SC-13 for one login.
///
/// The current partner is the first active one the server lists, the same one
/// new conversations are created for. A null profile with a `ready` status only
/// means the server has no usable profile; it cannot tell "no records", "being
/// built" and "outdated" apart, so none of those is guessed.
class PersonaController extends ChangeNotifier {
  PersonaController(this.service);
  final PersonaService service;

  LoadStatus _personasStatus = LoadStatus.idle;
  LoadStatus _profileStatus = LoadStatus.idle;
  RegenerationStatus _regeneration = RegenerationStatus.idle;
  List<Persona> _personas = const [];
  StyleProfile? _profile;
  int _personasRequest = 0, _profileRequest = 0;
  bool _disposed = false;

  LoadStatus get personasStatus => _personasStatus;
  LoadStatus get profileStatus => _profileStatus;
  RegenerationStatus get regeneration => _regeneration;
  List<Persona> get personas => _personas;
  StyleProfile? get profile => _profile;
  Persona? get current => _personas.isEmpty ? null : _personas.first;

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  /// Loads the partners and, if there is one, its profile.
  Future<void> load() async {
    final request = ++_personasRequest;
    _personasStatus = LoadStatus.loading;
    _notify();
    try {
      final items = await service.list();
      if (_disposed || request != _personasRequest) return;
      _personas = items;
      _personasStatus = LoadStatus.ready;
    } catch (_) {
      if (_disposed || request != _personasRequest) return;
      _personasStatus = LoadStatus.failed;
    }
    _notify();
    if (_personasStatus == LoadStatus.ready) await loadProfile();
  }

  Future<void> loadProfile() async {
    final persona = current;
    final request = ++_profileRequest;
    if (persona == null) {
      _profile = null;
      _profileStatus = LoadStatus.idle;
      _notify();
      return;
    }
    _profileStatus = LoadStatus.loading;
    _notify();
    try {
      final result = await service.profile(persona.id);
      if (_staleProfile(request, persona.id)) return;
      _profile = result;
      _profileStatus = LoadStatus.ready;
    } catch (_) {
      if (_staleProfile(request, persona.id)) return;
      _profileStatus = LoadStatus.failed;
    }
    _notify();
  }

  bool _staleProfile(int request, String personaId) =>
      _disposed || request != _profileRequest || current?.id != personaId;

  /// Creates a partner (SC-13, new). Throws on failure and changes nothing.
  Future<Persona> create(String name, {required String personaId}) async {
    final persona = await service.create(name, personaId: personaId);
    if (!_disposed) {
      _personas = [persona, ..._personas.where((p) => p.id != persona.id)];
      _personasStatus = LoadStatus.ready;
      _profile = null;
      _profileStatus = LoadStatus.idle;
      _regeneration = RegenerationStatus.idle;
      _profileRequest++; // drop lookups made for the previous partner
      _notify();
    }
    return persona;
  }

  /// Renames the current partner (SC-13, edit). The profile items are kept.
  Future<Persona> rename(String name) async {
    final persona = current;
    if (persona == null) throw StateError('No conversation partner');
    final updated = await service.rename(persona.id, name);
    if (!_disposed) {
      _personas = [
        for (final item in _personas) item.id == updated.id ? updated : item,
      ];
      _notify();
    }
    return updated;
  }

  /// Builds the profile again. The previous profile stays usable meanwhile and
  /// after a failure.
  Future<void> regenerate() async {
    final persona = current;
    if (persona == null || _regeneration == RegenerationStatus.running) return;
    _regeneration = RegenerationStatus.running;
    _notify();
    try {
      final result = await service.regenerate(persona.id);
      if (_disposed || current?.id != persona.id) return;
      _profile = result;
      _profileStatus = LoadStatus.ready;
      _regeneration = RegenerationStatus.succeeded;
    } catch (_) {
      if (_disposed) return;
      _regeneration = RegenerationStatus.failed;
    }
    _notify();
  }

  /// Clears a finished result so an old banner is not shown on the next visit.
  void acknowledgeRegeneration() {
    if (_regeneration == RegenerationStatus.running ||
        _regeneration == RegenerationStatus.idle) {
      return;
    }
    _regeneration = RegenerationStatus.idle;
    _notify();
  }
}
