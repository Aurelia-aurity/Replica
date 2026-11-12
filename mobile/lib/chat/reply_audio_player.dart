import 'dart:async';

import 'package:audioplayers/audioplayers.dart';
import 'package:audio_session/audio_session.dart' as session;

import 'chat_reply.dart';
import 'reply_audio_source.dart';

class AudioPlaybackInterrupted implements Exception {
  const AudioPlaybackInterrupted();
}

abstract class ReplyAudioPlayer {
  Stream<void> get completed;
  Stream<Object> get errors;
  Future<void> play(SpeechAudio audio);
  Future<void> stop();
  Future<void> dispose();
}

abstract class InteractiveReplyAudioPlayer implements ReplyAudioPlayer {
  Stream<Duration> get position;
  Stream<Duration> get duration;
  Future<void> pause();
  Future<void> resume();
  Future<void> seek(Duration value);
}

class DeviceReplyAudioPlayer implements InteractiveReplyAudioPlayer {
  AudioPlayer? _player;
  final _completed = StreamController<void>.broadcast();
  final _errors = StreamController<Object>.broadcast();
  final _position = StreamController<Duration>.broadcast();
  final _duration = StreamController<Duration>.broadcast();
  final _subscriptions = <StreamSubscription<dynamic>>[];
  Future<void> Function()? _cleanup;
  bool _disposed = false;
  bool _active = false;
  int _generation = 0;
  session.AudioSession? _session;

  Future<void> _activate() async {
    if (_session == null) {
      final audioSession = _session = await session.AudioSession.instance;
      _subscriptions.add(
        audioSession.interruptionEventStream.listen((event) {
          if (event.begin) _interrupted();
        }),
      );
      _subscriptions.add(
        audioSession.becomingNoisyEventStream.listen((_) {
          _interrupted();
        }),
      );
    }
    await _session!.configure(const session.AudioSessionConfiguration.speech());
    if (!await _session!.setActive(true)) {
      throw const AudioPlaybackInterrupted();
    }
    _active = true;
  }

  void _interrupted() {
    if (!_active || _disposed) return;
    _active = false;
    // Never resume automatically when focus returns. The next play action
    // goes through the screen's synthetic-voice disclosure again.
    _errors.add(const AudioPlaybackInterrupted());
    unawaited(stop().catchError((Object _) {}));
  }

  @override
  Stream<void> get completed => _completed.stream;
  @override
  Stream<Object> get errors => _errors.stream;
  @override
  Stream<Duration> get position => _position.stream;
  @override
  Stream<Duration> get duration => _duration.stream;
  @override
  Future<void> pause() async {
    await _player?.pause();
    _active = false;
    await _session?.setActive(false);
  }

  @override
  Future<void> resume() async {
    if (_disposed) return;
    final generation = _generation;
    await _activate();
    if (_disposed || generation != _generation) {
      await _session?.setActive(false);
      return;
    }
    await _player?.resume();
  }

  @override
  Future<void> seek(Duration value) async {
    if (!_disposed) await _player?.seek(value);
  }

  @override
  Future<void> play(SpeechAudio audio) async {
    await stop();
    final generation = _generation;
    if (_disposed) return;
    if (_player == null) {
      _player = AudioPlayer();
      _subscriptions.add(
        _player!.onPositionChanged.listen((value) {
          if (!_disposed) _position.add(value);
        }),
      );
      _subscriptions.add(
        _player!.onDurationChanged.listen((value) {
          if (!_disposed) _duration.add(value);
        }),
      );
      _subscriptions.add(
        _player!.onPlayerComplete.listen((_) {
          if (!_disposed) {
            _active = false;
            unawaited(_session?.setActive(false));
            _completed.add(null);
          }
        }),
      );
      _subscriptions.add(
        _player!.eventStream.listen(
          (_) {},
          onError: (Object error) {
            if (!_disposed) _errors.add(error);
          },
        ),
      );
    }
    // audio_session owns Android focus so the player cannot auto-resume on
    // focus gain. iOS uses the spoken-audio session configured below.
    await _player!.setAudioContext(
      AudioContext(
        android: const AudioContextAndroid(audioFocus: AndroidAudioFocus.none),
      ),
    );
    await _activate();
    if (_disposed || generation != _generation) {
      await _session?.setActive(false);
      return;
    }
    final source = await makeReplyAudioSource(audio);
    if (_disposed || generation != _generation) {
      await source.cleanup();
      return;
    }
    _cleanup = source.cleanup;
    await _player!.play(source.source);
    if (_disposed || generation != _generation) await stop();
  }

  @override
  Future<void> stop() async {
    _generation++;
    _active = false;
    try {
      await _player?.stop();
    } finally {
      await _session?.setActive(false);
      final cleanup = _cleanup;
      _cleanup = null;
      await cleanup?.call();
    }
  }

  @override
  Future<void> dispose() async {
    _disposed = true;
    try {
      await stop();
    } finally {
      for (final subscription in _subscriptions) {
        await subscription.cancel();
      }
      await _player?.dispose();
      await _completed.close();
      await _errors.close();
      await _position.close();
      await _duration.close();
    }
  }
}
