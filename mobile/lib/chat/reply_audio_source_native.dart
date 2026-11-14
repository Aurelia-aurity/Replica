import 'dart:io';

import 'package:audioplayers/audioplayers.dart';
import 'package:path_provider/path_provider.dart';

import 'chat_reply.dart';

Future<({Source source, Future<void> Function() cleanup})> makeReplyAudioSource(
  SpeechAudio audio,
) async {
  if (!Platform.isIOS && !Platform.isMacOS && !Platform.isLinux) {
    return (
      source: BytesSource(audio.bytes, mimeType: audio.mimeType),
      cleanup: () async {},
    );
  }
  // These platforms need a local file. Own its lifecycle instead of leaving
  // the plugin's hashed temporary file behind after playback.
  final directory = await (await getTemporaryDirectory()).createTemp(
    'replica-tts-',
  );
  try {
    final file = File(
      '${directory.path}/reply.${audio.mimeType == 'audio/wav' ? 'wav' : 'mp3'}',
    );
    await file.writeAsBytes(audio.bytes, flush: true);
    return (
      source: DeviceFileSource(file.path, mimeType: audio.mimeType),
      cleanup: () async {
        if (await directory.exists()) await directory.delete(recursive: true);
      },
    );
  } catch (_) {
    await directory.delete(recursive: true);
    rethrow;
  }
}
