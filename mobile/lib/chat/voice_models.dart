import 'dart:convert';
import 'dart:typed_data';

import '../services/backend_service.dart';
import 'chat_reply.dart';

const maxVoiceBytes = 25 * 1024 * 1024;
const maxVoiceDuration = Duration(minutes: 1);

class VoiceRecording {
  const VoiceRecording({
    required this.bytes,
    required this.duration,
    this.fileName = 'question.wav',
    this.mimeType = 'audio/wav',
  });
  final Uint8List bytes;
  final Duration duration;
  final String fileName, mimeType;
  void validate() {
    if (bytes.isEmpty) {
      throw const BackendFailure(
        BackendFailureKind.generic,
        code: 'EMPTY_FILE',
        stage: 'stt',
      );
    }
    if (bytes.length > maxVoiceBytes) {
      throw const BackendFailure(
        BackendFailureKind.generic,
        code: 'FILE_TOO_LARGE',
        stage: 'stt',
      );
    }
    if (!fileName.toLowerCase().endsWith('.wav') || mimeType != 'audio/wav') {
      throw const BackendFailure(
        BackendFailureKind.generic,
        code: 'UNSUPPORTED_AUDIO_FORMAT',
        stage: 'stt',
      );
    }
    if (duration < const Duration(milliseconds: 300) ||
        duration > maxVoiceDuration) {
      throw const BackendFailure(
        BackendFailureKind.generic,
        code: 'INVALID_AUDIO_DURATION',
        stage: 'stt',
      );
    }
    // Our capture/upload contract is canonical PCM16 WAV, not an arbitrary
    // file renamed to .wav. Validate before allocating the multipart request.
    if (bytes.length <= 44 ||
        ascii.decode(bytes.sublist(0, 4), allowInvalid: true) != 'RIFF' ||
        ascii.decode(bytes.sublist(8, 12), allowInvalid: true) != 'WAVE' ||
        ascii.decode(bytes.sublist(12, 16), allowInvalid: true) != 'fmt ' ||
        ascii.decode(bytes.sublist(36, 40), allowInvalid: true) != 'data') {
      throw const FormatException('Invalid WAV recording');
    }
    final header = ByteData.sublistView(bytes);
    final channels = header.getUint16(22, Endian.little);
    final rate = header.getUint32(24, Endian.little);
    final dataSize = bytes.length - 44;
    if (header.getUint32(4, Endian.little) != bytes.length - 8 ||
        header.getUint32(16, Endian.little) != 16 ||
        header.getUint16(20, Endian.little) != 1 ||
        channels < 1 ||
        channels > 2 ||
        rate < 8000 ||
        rate > 192000 ||
        header.getUint16(34, Endian.little) != 16 ||
        header.getUint16(32, Endian.little) != channels * 2 ||
        header.getUint32(28, Endian.little) != rate * channels * 2 ||
        header.getUint32(40, Endian.little) != dataSize ||
        dataSize % (channels * 2) != 0 ||
        dataSize * 10 < rate * channels * 2 * 3 ||
        dataSize > rate * channels * 2 * 60) {
      throw const FormatException('Invalid PCM recording');
    }
  }
}

Uint8List pcmToWave(Uint8List pcm, {int sampleRate = 16000, int channels = 1}) {
  if (channels < 1 ||
      sampleRate < 1 ||
      pcm.isEmpty ||
      pcm.length % (channels * 2) != 0) {
    throw const FormatException('Invalid PCM');
  }
  final wave = Uint8List(44 + pcm.length);
  final header = ByteData.sublistView(wave);
  void text(int offset, String value) =>
      wave.setRange(offset, offset + value.length, ascii.encode(value));
  text(0, 'RIFF');
  header.setUint32(4, 36 + pcm.length, Endian.little);
  text(8, 'WAVE');
  text(12, 'fmt ');
  header.setUint32(16, 16, Endian.little);
  header.setUint16(20, 1, Endian.little);
  header.setUint16(22, channels, Endian.little);
  header.setUint32(24, sampleRate, Endian.little);
  header.setUint32(28, sampleRate * channels * 2, Endian.little);
  header.setUint16(32, channels * 2, Endian.little);
  header.setUint16(34, 16, Endian.little);
  text(36, 'data');
  header.setUint32(40, pcm.length, Endian.little);
  wave.setRange(44, wave.length, pcm);
  return wave;
}

class VoiceReply {
  const VoiceReply({
    required this.transcript,
    required this.reply,
    this.audio,
    this.ttsFailed = false,
    this.preview = false,
  });
  final String transcript;
  final ChatReply reply;
  final SpeechAudio? audio;
  final bool ttsFailed, preview;

  factory VoiceReply.fromJson(Map<String, dynamic> json, String turnId) {
    if (json['transcript'] is! String ||
        (json['transcript'] as String).trim().isEmpty) {
      throw const BackendFailure(
        BackendFailureKind.generic,
        code: 'EMPTY_TRANSCRIPT',
        retryable: true,
        stage: 'stt',
      );
    }
    final reply = ChatReply.fromJson(json, expectedTurnId: turnId);
    if (!['ready', 'failed'].contains(json['tts_status'])) {
      throw const FormatException('Invalid TTS status');
    }
    SpeechAudio? audio;
    if (json['tts_status'] == 'ready') {
      try {
        audio = SpeechAudio.fromJson(json, turnId);
      } catch (_) {
        /* Keep a validated text answer even if audio is unusable. */
      }
    }
    return VoiceReply(
      transcript: (json['transcript'] as String).trim(),
      reply: reply,
      audio: audio,
      ttsFailed: audio == null,
    );
  }
}
