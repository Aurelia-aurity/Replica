import 'package:audioplayers/audioplayers.dart';

import 'chat_reply.dart';

Future<({Source source, Future<void> Function() cleanup})> makeReplyAudioSource(
  SpeechAudio audio,
) async => (
  source: BytesSource(audio.bytes, mimeType: audio.mimeType),
  cleanup: () async {},
);
