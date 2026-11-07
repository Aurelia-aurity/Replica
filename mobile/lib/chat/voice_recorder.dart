import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:record/record.dart';
import 'package:permission_handler/permission_handler.dart';

import 'voice_models.dart';

enum CaptureFailure { permission, empty, tooLarge, unavailable }

abstract class VoiceRecorder {
  Stream<double> get levels;
  Stream<Object> get errors;
  Future<void> start();
  Future<VoiceRecording> stop();
  Future<void> cancel();
  Future<bool> openSettings();
  Future<void> dispose();
}

class DeviceVoiceRecorder implements VoiceRecorder {
  AudioRecorder? _recorder;
  final _levels = StreamController<double>.broadcast();
  final _errors = StreamController<Object>.broadcast();
  StreamSubscription<Uint8List>? _subscription;
  final _clock = Stopwatch();
  BytesBuilder _pcm = BytesBuilder(copy: false);
  Completer<void>? _done;
  Object? _error;
  bool _disposed = false;
  int _peak = 0;
  int _sampleRate = 16000, _channels = 1;
  @override
  Stream<double> get levels => _levels.stream;
  @override
  Stream<Object> get errors => _errors.stream;

  @override
  Future<void> start() async {
    if (_disposed) throw CaptureFailure.unavailable;
    final recorder = _recorder ??= AudioRecorder();
    if (!await recorder.hasPermission()) throw CaptureFailure.permission;
    if (_disposed) return;
    _pcm = BytesBuilder(copy: false);
    _peak = 0;
    _error = null;
    _sampleRate = 16000;
    _channels = 1;
    await recorder.setOnConfigChanged((config) {
      _sampleRate = config.sampleRate;
      _channels = config.numChannels;
    });
    final stream = await recorder.startStream(
      const RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: 16000,
        numChannels: 1,
      ),
    );
    if (_disposed) {
      await recorder.cancel();
      return;
    }
    final done = _done = Completer<void>();
    _clock
      ..reset()
      ..start();
    _subscription = stream.listen(
      (chunk) {
        if (_disposed || _error != null) return;
        if (_pcm.length + chunk.length > maxVoiceBytes - 44) {
          _error = CaptureFailure.tooLarge;
          _errors.add(_error!);
          return;
        }
        _pcm.add(chunk);
        final samples = ByteData.sublistView(chunk);
        var peak = 0;
        for (var i = 0; i + 1 < chunk.length; i += 2) {
          final sample = samples.getInt16(i, Endian.little).abs();
          if (sample > peak) peak = sample;
        }
        if (peak > _peak) _peak = peak;
        _levels.add((peak / 32768).clamp(0, 1));
      },
      onError: (Object _) {
        _error = CaptureFailure.unavailable;
        if (!_disposed) _errors.add(_error!);
      },
      onDone: () {
        if (!done.isCompleted) done.complete();
      },
    );
  }

  @override
  Future<VoiceRecording> stop() async {
    await _recorder?.stop();
    _clock.stop();
    if (_done != null) {
      await _done!.future.timeout(const Duration(seconds: 2), onTimeout: () {});
    }
    await _subscription?.cancel();
    _subscription = null;
    if (_error != null) throw _error!;
    final pcm = _pcm.takeBytes();
    if (pcm.isEmpty || _clock.elapsedMilliseconds < 300 || _peak < 8) {
      throw CaptureFailure.empty;
    }
    return VoiceRecording(
      bytes: pcmToWave(pcm, sampleRate: _sampleRate, channels: _channels),
      duration: _clock.elapsed,
    );
  }

  @override
  Future<void> cancel() async {
    _clock.stop();
    await _recorder?.cancel();
    await _subscription?.cancel();
    _subscription = null;
    _pcm.clear();
  }

  @override
  Future<bool> openSettings() async {
    if (kIsWeb ||
        ![
          TargetPlatform.android,
          TargetPlatform.iOS,
        ].contains(defaultTargetPlatform)) {
      return false;
    }
    return openAppSettings();
  }

  @override
  Future<void> dispose() async {
    _disposed = true;
    try {
      await cancel();
    } finally {
      await _recorder?.dispose();
      await _levels.close();
      await _errors.close();
    }
  }
}

/// Exercises the flow without accessing a microphone or saving user audio.
class PreviewVoiceRecorder implements VoiceRecorder {
  @override
  Stream<double> get levels => const Stream.empty();
  @override
  Stream<Object> get errors => const Stream.empty();
  @override
  Future<void> start() async {}
  @override
  Future<VoiceRecording> stop() async => VoiceRecording(
    bytes: pcmToWave(Uint8List(32000)),
    duration: const Duration(seconds: 1),
  );
  @override
  Future<void> cancel() async {}
  @override
  Future<bool> openSettings() async => false;
  @override
  Future<void> dispose() async {}
}
