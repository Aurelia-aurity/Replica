import 'dart:async';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/foundation.dart';

/// Where the original audio of a record can be streamed from.
///
/// The draft API has no endpoint for this; a service supplies it only after the
/// app and backend agree on one. [simulated] sources only run a silent clock so
/// previews can show the player without any real file.
class RecordAudioSource {
  const RecordAudioSource({this.url, this.duration, this.simulated = false})
    : assert(simulated || url != null);
  final Uri? url;
  final Duration? duration;
  final bool simulated;
}

abstract class RecordPlaybackEngine {
  Stream<Duration> get position;
  Stream<Duration> get duration;
  Stream<void> get completed;
  Stream<Object> get errors;
  Future<void> open(RecordAudioSource source);
  Future<void> play();
  Future<void> pause();
  Future<void> seek(Duration value);
  Future<void> dispose();
}

class DevicePlaybackEngine implements RecordPlaybackEngine {
  DevicePlaybackEngine([AudioPlayer? player])
    : _player = player ?? AudioPlayer() {
    _subscriptions
      ..add(_player.onPositionChanged.listen(_position.add))
      ..add(_player.onDurationChanged.listen(_duration.add))
      ..add(_player.onPlayerComplete.listen((_) => _completed.add(null)))
      ..add(_player.eventStream.listen((_) {}, onError: _errors.add));
  }
  final AudioPlayer _player;
  final _position = StreamController<Duration>.broadcast();
  final _duration = StreamController<Duration>.broadcast();
  final _completed = StreamController<void>.broadcast();
  final _errors = StreamController<Object>.broadcast();
  final _subscriptions = <StreamSubscription<dynamic>>[];
  @override
  Stream<Duration> get position => _position.stream;
  @override
  Stream<Duration> get duration => _duration.stream;
  @override
  Stream<void> get completed => _completed.stream;
  @override
  Stream<Object> get errors => _errors.stream;
  @override
  Future<void> open(RecordAudioSource source) =>
      _player.setSourceUrl(source.url.toString());
  @override
  Future<void> play() => _player.resume();
  @override
  Future<void> pause() => _player.pause();
  @override
  Future<void> seek(Duration value) => _player.seek(value);
  @override
  Future<void> dispose() async {
    for (final subscription in _subscriptions) {
      await subscription.cancel();
    }
    await _player.dispose();
    await _position.close();
    await _duration.close();
    await _completed.close();
    await _errors.close();
  }
}

/// A silent clock for previews. It never touches audio or any file.
class SimulatedPlaybackEngine implements RecordPlaybackEngine {
  SimulatedPlaybackEngine({
    this.tick = const Duration(milliseconds: 250),
    this.fallbackDuration = const Duration(seconds: 192),
  });
  final Duration tick;
  final Duration fallbackDuration;
  final _position = StreamController<Duration>.broadcast();
  final _duration = StreamController<Duration>.broadcast();
  final _completed = StreamController<void>.broadcast();
  final _errors = StreamController<Object>.broadcast();
  Timer? _timer;
  Duration _at = Duration.zero;
  late Duration _total = fallbackDuration;
  @override
  Stream<Duration> get position => _position.stream;
  @override
  Stream<Duration> get duration => _duration.stream;
  @override
  Stream<void> get completed => _completed.stream;
  @override
  Stream<Object> get errors => _errors.stream;
  @override
  Future<void> open(RecordAudioSource source) async {
    _total = source.duration ?? fallbackDuration;
    _duration.add(_total);
  }

  @override
  Future<void> play() async {
    _timer?.cancel();
    _timer = Timer.periodic(tick, (_) {
      _at += tick;
      if (_at >= _total) {
        _at = _total;
        _timer?.cancel();
        _position.add(_at);
        _completed.add(null);
        return;
      }
      _position.add(_at);
    });
  }

  @override
  Future<void> pause() async => _timer?.cancel();
  @override
  Future<void> seek(Duration value) async {
    _at = value > _total ? _total : value;
    _position.add(_at);
  }

  @override
  Future<void> dispose() async {
    _timer?.cancel();
    await _position.close();
    await _duration.close();
    await _completed.close();
    await _errors.close();
  }
}

enum PlayerPhase { idle, loading, ready, failed }

/// Drives the SC-05 player bar. The audio source is resolved on first use so
/// opening a record never requests a file the user does not play.
class RecordPlayerController extends ChangeNotifier {
  RecordPlayerController({
    required this.resolve,
    RecordPlaybackEngine Function(RecordAudioSource source)? engineFactory,
  }) : _engineFactory = engineFactory ?? _defaultEngine;

  static RecordPlaybackEngine _defaultEngine(RecordAudioSource source) =>
      source.simulated ? SimulatedPlaybackEngine() : DevicePlaybackEngine();

  final Future<RecordAudioSource> Function() resolve;
  final RecordPlaybackEngine Function(RecordAudioSource) _engineFactory;
  RecordPlaybackEngine? _engine;
  final _subscriptions = <StreamSubscription<dynamic>>[];
  Future<void>? _opening;
  bool _disposed = false;
  PlayerPhase phase = PlayerPhase.idle;
  bool playing = false;
  bool simulated = false;
  Duration position = Duration.zero;
  Duration? duration;

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  Future<void> _ensureOpen() => _opening ??= _open();

  Future<void> _open() async {
    phase = PlayerPhase.loading;
    _changed();
    try {
      final source = await resolve();
      if (_disposed) return;
      simulated = source.simulated;
      duration = source.duration;
      final engine = _engine = _engineFactory(source);
      _subscriptions
        ..add(
          engine.position.listen((value) {
            position = value;
            _changed();
          }),
        )
        ..add(
          engine.duration.listen((value) {
            duration = value;
            _changed();
          }),
        )
        ..add(
          engine.completed.listen((_) {
            playing = false;
            position = duration ?? position;
            _changed();
          }),
        )
        ..add(engine.errors.listen((_) => _fail()));
      await engine.open(source);
      if (_disposed) return;
      phase = PlayerPhase.ready;
    } catch (_) {
      _fail();
    }
    _changed();
  }

  void _fail() {
    if (_disposed) return;
    phase = PlayerPhase.failed;
    playing = false;
    _opening = null; // a later tap may try again
    _changed();
  }

  Future<void> toggle() async {
    if (phase == PlayerPhase.loading) return;
    await _ensureOpen();
    final engine = _engine;
    if (_disposed || engine == null || phase != PlayerPhase.ready) return;
    try {
      if (playing) {
        await engine.pause();
        playing = false;
      } else {
        if (duration != null && position >= duration!) {
          await engine.seek(Duration.zero);
          position = Duration.zero;
        }
        await engine.play();
        playing = true;
      }
    } catch (_) {
      _fail();
    }
    _changed();
  }

  Future<void> seekTo(Duration value, {bool play = false}) async {
    if (phase == PlayerPhase.loading) return;
    await _ensureOpen();
    final engine = _engine;
    if (_disposed || engine == null || phase != PlayerPhase.ready) return;
    try {
      final limit = duration;
      final target = limit != null && value > limit ? limit : value;
      await engine.seek(target);
      position = target;
      if (play && !playing) {
        await engine.play();
        playing = true;
      }
    } catch (_) {
      _fail();
    }
    _changed();
  }

  Future<void> pause() async {
    final engine = _engine;
    if (engine == null || !playing) return;
    try {
      await engine.pause();
    } catch (_) {
      /* The player is going away or already stopped. */
    }
    playing = false;
    _changed();
  }

  @override
  void dispose() {
    _disposed = true;
    for (final subscription in _subscriptions) {
      unawaited(subscription.cancel());
    }
    final engine = _engine;
    if (engine != null) unawaited(engine.dispose());
    super.dispose();
  }
}
