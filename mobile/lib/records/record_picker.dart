import 'package:file_selector/file_selector.dart';

import 'record_models.dart';

typedef RecordPicker = Future<RecordFile?> Function();

Future<RecordFile?> pickRecordFile() async {
  final file = await openFile(
    acceptedTypeGroups: const [
      XTypeGroup(
        label: '텍스트·음성 기록',
        extensions: ['txt', 'wav', 'mp3', 'm4a'],
        uniformTypeIdentifiers: [
          'public.plain-text',
          'com.microsoft.waveform-audio',
          'public.mp3',
          'com.apple.m4a-audio',
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
