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

class DeviceReplyAudioPlayer implements ReplyAudioPlayer {
  AudioPlayer? _player;
  final _completed = StreamController<void>.broadcast();
  final _errors = StreamController<Object>.broadcast();
  final _subscriptions = <StreamSubscription<dynamic>>[];
  Future<void> Function()? _cleanup;
  bool _disposed = false;
  @override
  Stream<void> get completed => _completed.stream;
  @override
  Stream<Object> get errors => _errors.stream;

  @override
  Future<void> play(SpeechAudio audio) async {
    await stop();
    if (_disposed) return;
    if (_player == null) {
      _player = AudioPlayer();
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
    }
  }
}
