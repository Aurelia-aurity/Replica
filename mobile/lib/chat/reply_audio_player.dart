import 'dart:async';

import 'package:audioplayers/audioplayers.dart';

import 'chat_reply.dart';
import 'reply_audio_source.dart';

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
  }

  @override
  Future<void> resume() async {
    if (!_disposed) await _player?.resume();
  }

  @override
  Future<void> seek(Duration value) async {
    if (!_disposed) await _player?.seek(value);
  }

  @override
  Future<void> play(SpeechAudio audio) async {
    await stop();
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
          if (!_disposed) _completed.add(null);
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
    final source = await makeReplyAudioSource(audio);
    if (_disposed) {
      await source.cleanup();
      return;
    }
    _cleanup = source.cleanup;
    await _player!.play(source.source);
    if (_disposed) await stop();
  }

  @override
  Future<void> stop() async {
    try {
      await _player?.stop();
    } finally {
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
