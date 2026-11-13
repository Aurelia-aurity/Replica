import 'package:file_selector/file_selector.dart';

import 'record_models.dart';

typedef RecordPicker = Future<RecordFile?> Function();

Future<RecordFile?> pickRecordFile({RecordKind? kind}) async {
  final file = await openFile(
    acceptedTypeGroups: [
      XTypeGroup(
        label: '텍스트·음성 기록',
        extensions: kind == RecordKind.text
            ? ['txt']
            : kind == RecordKind.audio
            ? ['wav', 'mp3', 'm4a']
            : ['txt', 'wav', 'mp3', 'm4a'],
        uniformTypeIdentifiers: [
          if (kind != RecordKind.audio) 'public.plain-text',
          if (kind != RecordKind.text) ...[
            'com.microsoft.waveform-audio',
            'public.mp3',
            'com.apple.m4a-audio',
          ],
        ],
      ),
    ],
  );
  if (file == null) return null;
  return RecordFile(
    name: file.name,
    sizeBytes: await file.length(),
    openRead: file.openRead,
  );
}
