import 'dart:async';

import 'package:flutter/foundation.dart';

import '../services/backend_service.dart';
import 'persona_controller.dart';
import 'persona_service.dart';
import 'style_profile_models.dart';

/// How often SC-07 rechecks a running analysis while it is open.
///
/// Interim app values: the contract does not set a polling interval or limit
/// yet (asked in PR #117). After [analysisCheckLimit] the screen stops checking
/// by itself and offers a manual check instead.
const analysisCheckInterval = Duration(seconds: 15);
const analysisCheckLimit = Duration(minutes: 10);

/// SC-07 state for one partner, owned by one visit of the screen.
class StyleProfileController extends ChangeNotifier {
  StyleProfileController(
    this.service,
    this.personaId, {
    String Function()? newOperationId,
  }) : _newOperationId = newOperationId ?? newPersonaId;

  final PersonaService service;
  final String personaId;
  final String Function() _newOperationId;

  LoadStatus _status = LoadStatus.idle;
  StyleProfileState? _state;
  bool _requesting = false;
  bool _watchExpired = false;
  int _request = 0;
  bool _disposed = false;
  Timer? _timer;
  int _checks = 0;

  /// Operation id of a request whose answer was lost; reused by its retry.
  String? _pendingOperationId;

  LoadStatus get status => _status;
  StyleProfileState? get state => _state;
  bool get requesting => _requesting;

  /// The last lookup failed because the backend does not serve the style
  /// profile yet ([apiNotProvided]); SC-07 then says so instead of offering a
  /// retry that cannot help.
  bool get notProvided => _notProvided;
  bool _notProvided = false;

  /// The automatic checks of a running analysis stopped at the limit.
  bool get watchExpired => _watchExpired;

  @override
  void dispose() {
    _disposed = true;
    _timer?.cancel();
    super.dispose();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  /// Loads the profile and analysis state. With [quiet] the state already on
  /// screen stays while loading and a failure keeps it (used by rechecks). A
  /// late answer to an older request is dropped.
  Future<void> load({bool quiet = false}) async {
    final request = ++_request;
    final keep = quiet && _state != null;
    if (!keep) {
      _status = LoadStatus.loading;
      _notify();
    }
    try {
      final result = await service.styleProfile(personaId);
      if (_disposed || request != _request) return;
      _state = result;
      _status = LoadStatus.ready;
      _notProvided = false;
    } catch (error) {
      if (_disposed || request != _request) return;
      if (!keep) {
        _status = LoadStatus.failed;
        _notProvided = error is BackendFailure && error.code == apiNotProvided;
      }
    }
    _watch();
    _notify();
  }

  /// Checks again by hand after the automatic checks stopped.
  Future<void> checkAgain() {
    _watchExpired = false;
    _checks = 0;
    return load(quiet: true);
  }

  /// Asks the server to analyze again. Throws on failure; the state on screen
  /// then stays. An answer lost on the way is retried with the same id.
  Future<void> requestAnalysis() async {
    if (_requesting) return;
    _requesting = true;
    _notify();
    final operationId = _pendingOperationId ??= _newOperationId();
    try {
      final analysis = await service.requestAnalysis(
        personaId,
        operationId: operationId,
      );
      _pendingOperationId = null;
      if (_disposed) return;
      _request++; // an older lookup must not bring back the previous state
      _state = (_state ?? const StyleProfileState()).withAnalysis(analysis);
      _status = LoadStatus.ready;
      _watchExpired = false;
      _checks = 0;
      _watch();
    } catch (error) {
      // Only an unanswered request may have started a job on the server.
      if (!(error is BackendFailure &&
          error.kind == BackendFailureKind.network)) {
        _pendingOperationId = null;
      }
      rethrow;
    } finally {
      _requesting = false;
      _notify();
    }
  }

  /// Rechecks while the analysis runs (or the profile is being built), up to
  /// [analysisCheckLimit].
  void _watch() {
    _timer?.cancel();
    _timer = null;
    if (_disposed || _state?.inProgress != true) {
      _checks = 0;
      return;
    }
    // Counted rather than timed, so the limit holds however long each answer
    // takes and the screen's clock is never consulted.
    if (_checks * analysisCheckInterval.inSeconds >=
        analysisCheckLimit.inSeconds) {
      _watchExpired = true;
      return;
    }
    _timer = Timer(analysisCheckInterval, () {
      _checks++;
      load(quiet: true);
    });
  }
}
