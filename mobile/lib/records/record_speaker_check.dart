import 'package:flutter/material.dart';

import '../services/backend_service.dart';
import '../shared/conversation_widgets.dart';
import 'deletion_sheet.dart';
import 'record_controller.dart';
import 'record_models.dart';
import 'record_service.dart';
import 'speaker_choice.dart';
import 'text_analysis.dart';

/// Continues a record that `/uploaded` left waiting for its speakers
/// (`status: uploading` with `error_code: speaker_*`), for example after the
/// app was closed. Opening it calls `/uploaded` once without a body to get the
/// speakers the server found; the choice is then sent with `/uploaded`. The
/// file is never sent again.
class SpeakerCheckController extends ChangeNotifier {
  SpeakerCheckController(this.service, this.entry);
  final RecordService service;
  final RecordEntry entry;

  bool busy = false;
  bool done = false;
  String? error;

  /// The speakers the server found; null until it answered with them.
  TextAnalysis? analysis;
  String? targetSpeaker;
  String? providerSpeaker;
  bool _disposed = false;

  bool get singleSpeaker => analysis?.singleSpeaker ?? false;
  bool get needsSpeakers => analysis?.needsSpeakerSelection ?? false;
  bool get needsProvider => analysis?.needsProviderSelection ?? false;

  bool get canSend =>
      !busy &&
      !done &&
      analysis != null &&
      (!needsSpeakers ||
          (targetSpeaker != null &&
              (!needsProvider || providerSpeaker != null)));

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  void selectTarget(String name) {
    if (busy || done || name == providerSpeaker) return;
    targetSpeaker = name;
    _changed();
  }

  void selectProvider(String name) {
    if (busy || done || !needsProvider || name == targetSpeaker) return;
    providerSpeaker = name;
    _changed();
  }

  /// Asks the server once for the speakers (called when the screen opens, or
  /// when the user asks to check again after a failure).
  Future<void> load() => _complete(null);

  /// Sends the choice. A value not needed is sent as null to clear it.
  Future<void> send() {
    if (!canSend) return Future.value();
    return _complete({
      'target_speaker': needsSpeakers ? targetSpeaker : null,
      'provider_speaker': needsProvider ? providerSpeaker : null,
    });
  }

  Future<void> _complete(
    Map<String, String?>? speakers, {
    bool cleared = false,
  }) async {
    if (busy || done) return;
    busy = true;
    error = null;
    _changed();
    try {
      await service.completeUpload(entry.id, speakers: speakers);
      if (_disposed) return;
      done = true;
    } on BackendFailure catch (failure) {
      if (_disposed) return;
      if (failure.code == 'SPEAKER_NOT_ALLOWED' && !cleared) {
        // The server needs no value there: clear it and call once more.
        busy = false;
        await _complete(
          failure.field == 'provider_speaker'
              ? {'provider_speaker': null}
              : {'target_speaker': null, 'provider_speaker': null},
          cleared: true,
        );
        return;
      }
      if (isSpeakerFailure(failure)) {
        _choose(failure.speakers ?? const []);
      } else {
        error = _message(failure);
      }
    } catch (_) {
      if (_disposed) return;
      error = '확인하지 못했어요. 다시 시도해 주세요.';
    } finally {
      if (!_disposed) {
        busy = false;
        _changed();
      }
    }
  }

  void _choose(List<String> names) {
    analysis = TextAnalysis.fromServer(names);
    if (!names.contains(targetSpeaker)) targetSpeaker = null;
    if (!needsProvider || !names.contains(providerSpeaker)) {
      providerSpeaker = null;
    }
  }

  String _message(BackendFailure failure) => switch (failure.code) {
    'UPLOAD_MISSING' => '원본 파일이 서버에 없어요. 이 기록을 삭제하고 새로 올려 주세요.',
    'CONFLICT' => '이 기록은 더 이상 이어서 올릴 수 없어요. 이 기록을 삭제하고 새로 올려 주세요.',
    'NOT_FOUND' => '기록을 찾을 수 없어요. 기록 목록을 새로고침해 주세요.',
    _ =>
      failure.kind == BackendFailureKind.network
          ? '연결 상태를 확인하고 다시 시도해 주세요.'
          : '확인하지 못했어요. 다시 시도해 주세요.',
  };
}

/// SC-03 → '확인 필요' record waiting for its speakers.
class RecordSpeakerCheckScreen extends StatefulWidget {
  const RecordSpeakerCheckScreen({
    super.key,
    required this.records,
    required this.entry,
  });
  final RecordController records;
  final RecordEntry entry;
  @override
  State<RecordSpeakerCheckScreen> createState() =>
      _RecordSpeakerCheckScreenState();
}

class _RecordSpeakerCheckScreenState extends State<RecordSpeakerCheckScreen> {
  late final check = SpeakerCheckController(
    widget.records.service,
    widget.entry,
  );

  @override
  void initState() {
    super.initState();
    // Once per opening: the server re-reads the whole original for it.
    check.load();
  }

  @override
  void dispose() {
    check.dispose();
    super.dispose();
  }

  Future<void> _delete() async {
    final deletion = widget.records.deletionFor(widget.entry.id);
    deletion.label = widget.entry.name;
    await showDeletionSheet(
      context,
      deletion,
      preview: widget.records.service.isPreview,
    );
    if (!mounted || deletion.job == null) return;
    Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: check,
    builder: (context, _) {
      final analysis = check.analysis;
      return PopScope(
        canPop: !check.busy,
        child: Scaffold(
          appBar: AppBar(title: const Text('화자 확인')),
          body: ListView(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
            children: [
              Text(
                widget.entry.name,
                style: const TextStyle(
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 8),
              const Text(
                '이 기록은 화자 확인이 필요해요. 서버가 파일에서 찾은 화자 중에서 골라 주세요. 파일은 다시 올리지 않아요.',
                style: speakerMutedText,
              ),
              if (check.busy && analysis == null)
                const Padding(
                  padding: EdgeInsets.only(top: 24),
                  child: Center(child: CircularProgressIndicator()),
                ),
              if (analysis != null && check.needsSpeakers)
                ...speakerChoiceSections(
                  speakers: analysis.speakers,
                  target: check.targetSpeaker,
                  provider: check.providerSpeaker,
                  locked: check.busy || check.done,
                  sendSelection: true,
                  onTarget: check.selectTarget,
                  onProvider: check.selectProvider,
                  singleNotice: '이 화자가 대화 상대일 때만 선택해 주세요. 대화 상대가 아니라면 선택하지 말고 이 기록을 삭제해 주세요.',
                ),
              if (check.error != null)
                Padding(
                  padding: const EdgeInsets.only(top: 16),
                  child: Semantics(
                    liveRegion: true,
                    child: Text(
                      check.error!,
                      style: const TextStyle(color: Color(0xFFB3261E)),
                    ),
                  ),
                ),
              if (check.done)
                const Padding(
                  padding: EdgeInsets.only(top: 16),
                  child: Text(
                    '화자 확인을 마쳤어요. 기록을 반영하고 있어요.',
                    style: TextStyle(
                      color: replicaGreen,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              if (!check.done)
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton(
                    key: const ValueKey('speaker-check-delete'),
                    onPressed: check.busy ? null : _delete,
                    child: const Text('이 기록 삭제'),
                  ),
                ),
            ],
          ),
          bottomNavigationBar: SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(20, 10, 20, 8),
              child: FilledButton(
                key: const ValueKey('speaker-check-send'),
                style: FilledButton.styleFrom(
                  backgroundColor: replicaGreen,
                  minimumSize: const Size.fromHeight(56),
                ),
                onPressed: check.done
                    ? () => Navigator.of(context).pop()
                    : analysis == null
                    ? (check.busy ? null : check.load)
                    : check.canSend
                    ? check.send
                    : null,
                child: Text(
                  check.done
                      ? '기록 목록 보기'
                      : analysis == null
                      ? (check.busy ? '확인하는 중…' : '다시 확인')
                      : '선택한 화자로 이어서 올리기',
                  style: const TextStyle(
                    fontSize: 17,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ),
          ),
        ),
      );
    },
  );
}
