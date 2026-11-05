import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/records/record_controller.dart';
import 'package:mobile/records/record_models.dart';
import 'package:mobile/records/record_playback.dart';
import 'package:mobile/records/record_review.dart';
import 'package:mobile/records/record_review_screen.dart';
import 'package:mobile/records/record_service.dart';
import 'package:mobile/records/transcript_segments.dart';
import 'package:mobile/services/backend_service.dart';
import 'package:mobile/shared/conversation_widgets.dart';

const _segments = [
  TranscriptSegment(
    text: '밥은 먹었나?',
    startMs: 4000,
    endMs: 11000,
    speaker: '엄마',
  ),
  TranscriptSegment(text: '김치 좀 싸 줄게.', startMs: 11000, endMs: 18000),
  TranscriptSegment(text: '아빠가 너 보고 싶다 카더라.', startMs: 18000),
  TranscriptSegment(
    text: '감기 조심하고 단디 챙겨 입어라.',
    startMs: 26000,
    uncertain: ['단디'],
  ),
];

class FakeEngine implements RecordPlaybackEngine {
  final positions = StreamController<Duration>.broadcast();
  final durations = StreamController<Duration>.broadcast();
  final done = StreamController<void>.broadcast();
  final failures = StreamController<Object>.broadcast();
  final seeks = <Duration>[];
  int plays = 0, pauses = 0;
  bool opened = false, disposed = false, failOpen = false;
  @override
  Stream<Duration> get position => positions.stream;
  @override
  Stream<Duration> get duration => durations.stream;
  @override
  Stream<void> get completed => done.stream;
  @override
  Stream<Object> get errors => failures.stream;
  @override
  Future<void> open(RecordAudioSource source) async {
    if (failOpen) throw StateError('cannot open');
    opened = true;
    durations.add(const Duration(seconds: 192));
  }

  @override
  Future<void> play() async => plays++;
  @override
  Future<void> pause() async => pauses++;
  @override
  Future<void> seek(Duration value) async => seeks.add(value);
  @override
  Future<void> dispose() async => disposed = true;
}

class Fixture extends PreviewRecordService {
  Fixture({this.segments = _segments, this.audio = true});
  List<TranscriptSegment>? segments;
  final bool audio;
  RecordStatus status = RecordStatus.ready;
  String? content;
  DateTime updated = DateTime.utc(2026, 11, 1);
  bool confirmed = false;
  int saves = 0, resolves = 0;
  String? saved;
  bool conflictOnSave = false;
  bool failAudio = false;
  String get body => content ?? segments!.map((s) => s.text).join('\n');
  RecordEntry get entry => RecordEntry(
    id: 'record',
    name: audio ? '음성메모.m4a' : '일기.txt',
    kind: audio ? RecordKind.audio : RecordKind.text,
    status: status,
    createdAt: DateTime.utc(2026, 10, 6),
    textConfirmed: confirmed,
  );
  TextVersion get version => TextVersion(
    id: 'v',
    recordId: 'record',
    revision: 1,
    content: body,
    status: confirmed ? 'confirmed' : 'draft',
    updatedAt: updated,
    confirmedAt: confirmed ? updated : null,
    segments: content == null ? segments : null,
  );
  @override
  bool get canPlayOriginal => audio;
  @override
  Future<RecordAudioSource> originalAudio(String recordId) async {
    resolves++;
    if (failAudio) throw StateError('no audio');
    return RecordAudioSource(
      url: Uri.parse('https://audio.invalid/original'),
      duration: const Duration(seconds: 192),
    );
  }

  @override
  Future<List<RecordEntry>> list() async => [entry];
  @override
  Future<RecordReview> review(String recordId) async => RecordReview(
    record: entry,
    version: status == RecordStatus.ready ? version : null,
  );
  @override
  Future<TextVersion> saveText(TextVersion v, String text) async {
    saves++;
    if (conflictOnSave) {
      conflictOnSave = false;
      throw const BackendFailure(BackendFailureKind.generic, code: 'CONFLICT');
    }
    saved = text;
    content = text;
    updated = updated.add(const Duration(seconds: 1));
    return version;
  }

  @override
  Future<TextVersion> confirmText(TextVersion v) async {
    confirmed = true;
    return version;
  }
}

Future<void> pumpReview(
  WidgetTester tester,
  Fixture service,
  RecordController records, {
  FakeEngine? engine,
  Duration poll = const Duration(seconds: 5),
  bool settle = true,
}) async {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = const Size(800, 1800);
  addTearDown(tester.view.resetDevicePixelRatio);
  addTearDown(tester.view.resetPhysicalSize);
  await tester.pumpWidget(
    MaterialApp(
      home: RecordReviewScreen(
        records: records,
        entry: service.entry,
        pollInterval: poll,
        playbackEngineFactory: engine == null ? null : (_) => engine,
      ),
    ),
  );
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    // An indeterminate progress bar never settles.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  group('segments', () {
    test('lenient parsing drops bad items and keeps good fields', () {
      final ok = TranscriptSegment.tryParse({
        'text': '안녕',
        'start_ms': 1500,
        'end_ms': 2500,
        'speaker': '엄마',
        'uncertain_words': ['단디', 3],
      })!;
      expect(ok.startMs, 1500);
      expect(ok.endMs, 2500);
      expect(ok.speaker, '엄마');
      expect(ok.uncertain, ['단디']);
      expect(TranscriptSegment.tryParse('x'), isNull);
      expect(TranscriptSegment.tryParse({'text': 3}), isNull);
      final odd = TranscriptSegment.tryParse({
        'text': 'a',
        'start_ms': 5000,
        'end_ms': 1000,
        'speaker': ' ',
      })!;
      expect(odd.endMs, isNull);
      expect(odd.speaker, isNull);
      expect(
        TranscriptSegment.tryParse({'text': 'a', 'start_ms': -1})!.startMs,
        isNull,
      );
    });

    test('server segments are used only when they reproduce the content', () {
      Map<String, Object?> json(String content, List<Object?>? segments) => {
        'id': 'v',
        'record_id': 'r',
        'revision': 1,
        'content': content,
        'status': 'draft',
        'updated_at': '2026-11-01T00:00:00Z',
        'segments': ?segments,
      };
      final good = TextVersion.fromJson(
        json('가\n나', [
          {'text': '가', 'start_ms': 0},
          {'text': '나', 'start_ms': 1000},
        ]),
      );
      expect(good.segments!.length, 2);
      final mismatch = TextVersion.fromJson(
        json('가\n나', [
          {'text': '가', 'start_ms': 0},
          {'text': '다', 'start_ms': 1000},
        ]),
      );
      expect(mismatch.segments, isNull);
      expect(TextVersion.fromJson(json('가', null)).segments, isNull);
      expect(TextVersion.fromJson(json('가', [])).segments, isNull);
    });

    test('editing keeps timing only while the segment count is unchanged', () {
      final same = TranscriptDocument.fromContent(
        '가\n나\n다\n라',
        base: _segments,
      );
      expect(same.segments[0].startMs, 4000);
      expect(same.segments[0].speaker, '엄마');
      expect(same.segments[3].uncertain, ['단디']);
      expect(same.segments[2].text, '다');
      final fewer = TranscriptDocument.fromContent('가\n나', base: _segments);
      expect(fewer.hasTimestamps, isFalse);
      expect(fewer.segments.length, 2);
      final lines = TranscriptDocument.fromContent('첫째\n둘째\n셋째');
      expect(lines.hasTimestamps, isFalse);
      expect(lines.content, '첫째\n둘째\n셋째');
    });

    test('active segment follows the position', () {
      const doc = TranscriptDocument(_segments);
      expect(doc.activeIndexAt(0), isNull);
      expect(doc.activeIndexAt(4000), 0);
      expect(doc.activeIndexAt(17999), 1);
      expect(doc.activeIndexAt(18000), 2);
      expect(doc.activeIndexAt(999999), 3);
      expect(formatClock(const Duration(seconds: 192)), '03:12');
      expect(formatClock(const Duration(seconds: 3700)), '1:01:40');
    });

    test('segment edits fold line breaks and are kept as drafts', () async {
      final service = Fixture();
      final drafts = <String, String>{};
      final c = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: drafts,
      );
      addTearDown(c.dispose);
      await c.load();
      c.editSegment(1, '김치\n두 통\r\n싸 줄게.');
      expect(c.content.split('\n').length, 4);
      expect(c.document.segments[1].text, '김치 두 통 싸 줄게.');
      expect(c.document.segments[1].startMs, 11000);
      expect(drafts['record'], c.content);
      c.editSegment(9, '범위 밖');
      expect(c.content.split('\n').length, 4);
    });
  });

  group('player', () {
    test('opens lazily, plays, seeks and reports completion', () async {
      final engine = FakeEngine();
      var resolves = 0;
      final player = RecordPlayerController(
        resolve: () async {
          resolves++;
          return RecordAudioSource(
            url: Uri.parse('https://audio.invalid/a'),
            duration: const Duration(seconds: 192),
          );
        },
        engineFactory: (_) => engine,
      );
      addTearDown(player.dispose);
      expect(resolves, 0);
      expect(player.phase, PlayerPhase.idle);
      await player.toggle();
      expect(resolves, 1);
      expect(player.phase, PlayerPhase.ready);
      expect(player.playing, isTrue);
      expect(engine.plays, 1);
      engine.positions.add(const Duration(seconds: 18));
      await pumpEventQueue();
      expect(player.position, const Duration(seconds: 18));
      await player.seekTo(const Duration(seconds: 500), play: true);
      expect(engine.seeks.last, const Duration(seconds: 192)); // clamped
      await player.toggle();
      expect(player.playing, isFalse);
      expect(engine.pauses, 1);
      await player.seekTo(const Duration(seconds: 26), play: true);
      expect(player.playing, isTrue);
      expect(resolves, 1);
      engine.done.add(null);
      await pumpEventQueue();
      expect(player.playing, isFalse);
    });

    test('a source that cannot be opened fails and can be retried', () async {
      final engines = <FakeEngine>[];
      var attempts = 0;
      final player = RecordPlayerController(
        resolve: () async {
          attempts++;
          return RecordAudioSource(url: Uri.parse('https://audio.invalid/a'));
        },
        engineFactory: (_) {
          final engine = FakeEngine()..failOpen = engines.isEmpty;
          engines.add(engine);
          return engine;
        },
      );
      addTearDown(player.dispose);
      await player.toggle();
      expect(player.phase, PlayerPhase.failed);
      expect(player.playing, isFalse);
      await player.toggle();
      expect(attempts, 2);
      expect(player.phase, PlayerPhase.ready);
      expect(player.playing, isTrue);
    });

    test('dispose releases the engine and ignores late events', () async {
      final engine = FakeEngine();
      final player = RecordPlayerController(
        resolve: () async =>
            RecordAudioSource(url: Uri.parse('https://audio.invalid/a')),
        engineFactory: (_) => engine,
      );
      await player.toggle();
      player.dispose();
      await pumpEventQueue();
      expect(engine.disposed, isTrue);
      engine.positions.add(const Duration(seconds: 3));
      await pumpEventQueue();
    });

    test('the simulated engine only keeps a silent clock', () async {
      final engine = SimulatedPlaybackEngine(
        tick: const Duration(milliseconds: 10),
      );
      final seen = <Duration>[];
      engine.position.listen(seen.add);
      await engine.open(
        const RecordAudioSource(
          simulated: true,
          duration: Duration(milliseconds: 40),
        ),
      );
      final finished = engine.completed.first;
      await engine.play();
      await finished;
      expect(seen.last, const Duration(milliseconds: 40));
      await engine.dispose();
    });
  });

  group('review screen', () {
    testWidgets('timed segments seek and play the original audio', (
      tester,
    ) async {
      final service = Fixture();
      final records = RecordController(service);
      addTearDown(records.dispose);
      final engine = FakeEngine();
      await pumpReview(tester, service, records, engine: engine);
      expect(find.text('00:04'), findsOneWidget);
      expect(find.text('00:18'), findsOneWidget);
      expect(find.text('엄마'), findsOneWidget);
      expect(service.resolves, 0, reason: 'audio is requested on first play');
      await tester.tap(find.byKey(const ValueKey('segment-time-2')));
      await tester.pumpAndSettle();
      expect(service.resolves, 1);
      expect(engine.seeks, [const Duration(seconds: 18)]);
      expect(engine.plays, 1);
      engine.positions.add(const Duration(seconds: 27));
      await tester.pump();
      await tester.pump();
      expect(find.text('00:27'), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('player-toggle')));
      await tester.pumpAndSettle();
      expect(engine.pauses, 1);
    });

    testWidgets('uncertain words are highlighted', (tester) async {
      final service = Fixture();
      final records = RecordController(service);
      addTearDown(records.dispose);
      await pumpReview(tester, service, records, engine: FakeEngine());
      final spans = <TextSpan>[];
      for (final rich in tester.widgetList<RichText>(find.byType(RichText))) {
        rich.text.visitChildren((span) {
          if (span is TextSpan &&
              span.style?.backgroundColor == aiAmberBackground) {
            spans.add(span);
          }
          return true;
        });
      }
      expect(spans.map((s) => s.text), ['단디']);
    });

    testWidgets('editing one segment saves the whole transcript', (
      tester,
    ) async {
      final service = Fixture();
      final records = RecordController(service);
      addTearDown(records.dispose);
      await pumpReview(tester, service, records, engine: FakeEngine());
      await tester.tap(find.byKey(const ValueKey('segment-text-1')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('segment-editor-1')),
        '김치 두 통 싸 줄게.',
      );
      await tester.tap(find.byKey(const ValueKey('segment-done-1')));
      await tester.pumpAndSettle();
      expect(find.text('김치 두 통 싸 줄게.'), findsOneWidget);
      expect(records.reviewDrafts['record'], contains('김치 두 통'));
      await tester.tap(find.byKey(const ValueKey('transcript-confirm')));
      await tester.pumpAndSettle();
      expect(
        service.saved,
        '밥은 먹었나?\n김치 두 통 싸 줄게.\n아빠가 너 보고 싶다 카더라.\n감기 조심하고 단디 챙겨 입어라.',
      );
      expect(service.confirmed, isTrue);
    });

    testWidgets('without server timing there is no invented timestamp', (
      tester,
    ) async {
      final service = Fixture(
        segments: const [
          TranscriptSegment(text: '첫 줄'),
          TranscriptSegment(text: '둘째 줄'),
        ],
      );
      final records = RecordController(service);
      addTearDown(records.dispose);
      await pumpReview(tester, service, records, engine: FakeEngine());
      expect(find.textContaining('구간 시간 정보가 제공되지 않아'), findsOneWidget);
      expect(find.byKey(const ValueKey('segment-time-0')), findsNothing);
      expect(find.text('1'), findsOneWidget);
      expect(find.text('2'), findsOneWidget);
    });

    testWidgets(
      'a service without audio explains it instead of a dead button',
      (tester) async {
        final service = Fixture(audio: true);
        final records = RecordController(_NoAudio(service));
        addTearDown(records.dispose);
        await pumpReview(tester, service, records);
        expect(find.textContaining('원본 음성 재생은 아직 연결되지 않았어요'), findsOneWidget);
        expect(find.byKey(const ValueKey('player-toggle')), findsNothing);
      },
    );

    testWidgets('a failing audio source shows an error and stays usable', (
      tester,
    ) async {
      final service = Fixture()..failAudio = true;
      final records = RecordController(service);
      addTearDown(records.dispose);
      await pumpReview(tester, service, records, engine: FakeEngine());
      await tester.tap(find.byKey(const ValueKey('player-toggle')));
      await tester.pumpAndSettle();
      expect(find.textContaining('원본 음성을 불러오지 못했어요'), findsOneWidget);
      expect(find.byKey(const ValueKey('transcript-segments')), findsOneWidget);
    });

    testWidgets('a record that is still transcribing refreshes by itself', (
      tester,
    ) async {
      final service = Fixture()..status = RecordStatus.processing;
      final records = RecordController(service);
      addTearDown(records.dispose);
      await pumpReview(
        tester,
        service,
        records,
        engine: FakeEngine(),
        poll: const Duration(milliseconds: 200),
        settle: false,
      );
      expect(find.textContaining('전사 중이에요'), findsOneWidget);
      expect(find.byKey(const ValueKey('transcript-segments')), findsNothing);
      service.status = RecordStatus.ready;
      await tester.pump(const Duration(milliseconds: 250));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('transcript-segments')), findsOneWidget);
      expect(find.textContaining('전사 중이에요'), findsNothing);
    });
  });

  group('edit conflicts', () {
    test('a changed server text must be chosen explicitly', () async {
      final service = Fixture(audio: false)..conflictOnSave = true;
      final drafts = <String, String>{};
      final c = RecordReviewController(
        service: service,
        recordId: 'record',
        drafts: drafts,
      );
      addTearDown(c.dispose);
      await c.load();
      c.edit('내 수정');
      expect(await c.save(confirm: true), isFalse);
      expect(c.conflict, isTrue);
      // Meanwhile someone else saved a different text.
      service.content = '다른 사람의 수정';
      service.updated = service.updated.add(const Duration(minutes: 1));
      await c.load();
      expect(c.needsResolution, isTrue);
      expect(c.content, '내 수정');
      expect(c.canConfirm, isFalse);
      expect(await c.save(), isFalse);
      expect(service.saved, isNull);
      c.edit('무시되는 입력');
      expect(c.content, '내 수정');
      c.useServer();
      expect(c.content, '다른 사람의 수정');
      expect(drafts, isEmpty);
      expect(c.needsResolution, isFalse);
      expect(c.canConfirm, isTrue);
    });

    test(
      'keeping my edit saves over the newer server text on request',
      () async {
        final service = Fixture(audio: false)..conflictOnSave = true;
        final c = RecordReviewController(
          service: service,
          recordId: 'record',
          drafts: {},
        );
        addTearDown(c.dispose);
        await c.load();
        c.edit('내 수정');
        await c.save();
        service.content = '다른 사람의 수정';
        service.updated = service.updated.add(const Duration(minutes: 1));
        await c.load();
        c.keepMine();
        expect(await c.save(confirm: true), isTrue);
        expect(service.saved, '내 수정');
        expect(service.confirmed, isTrue);
      },
    );

    testWidgets('conflict banner offers both choices and blocks confirming', (
      tester,
    ) async {
      final service = Fixture(audio: false)..conflictOnSave = true;
      final records = RecordController(service);
      addTearDown(records.dispose);
      await pumpReview(tester, service, records);
      await tester.tap(find.byKey(const ValueKey('segment-text-0')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('segment-editor-0')),
        '내 수정',
      );
      await tester.tap(find.byKey(const ValueKey('segment-done-0')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('transcript-confirm')));
      await tester.pumpAndSettle();
      expect(find.textContaining('다른 곳에서 전사문이 바뀌었어요'), findsOneWidget);
      service.content = '서버 최신';
      service.updated = service.updated.add(const Duration(minutes: 1));
      await tester.tap(find.text('최신 내용 다시 확인'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('conflict-resolution')), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('transcript-confirm')),
            )
            .onPressed,
        isNull,
      );
      await tester.tap(find.byKey(const ValueKey('resolve-server')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('conflict-resolution')), findsNothing);
      expect(find.text('서버 최신'), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(
              find.byKey(const ValueKey('transcript-confirm')),
            )
            .onPressed,
        isNotNull,
      );
    });
  });
}

/// A real-looking service whose audio is not connected.
class _NoAudio extends PreviewRecordService {
  _NoAudio(this.inner);
  final Fixture inner;
  @override
  bool get canPlayOriginal => false;
  @override
  Future<List<RecordEntry>> list() => inner.list();
  @override
  Future<RecordReview> review(String recordId) => inner.review(recordId);
  @override
  Future<TextVersion> saveText(TextVersion v, String text) =>
      inner.saveText(v, text);
  @override
  Future<TextVersion> confirmText(TextVersion v) => inner.confirmText(v);
}
