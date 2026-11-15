import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../services/backend_service.dart';
import '../shared/conversation_widgets.dart';
import 'persona_controller.dart';
import 'persona_flow.dart';
import 'persona_models.dart';
import 'style_profile_controller.dart';
import 'style_profile_models.dart';

/// Opens SC-04 (record upload) for [persona]. Records are built in their own
/// work item; until one is passed the screen shows a notice instead.
typedef RecordUploadLauncher = Future<void> Function(
  BuildContext context,
  Persona persona,
);

/// "10월 6일", with the year when it is not the current one.
String profileDate(DateTime value, DateTime now) {
  final date = value.toLocal();
  final day = '${date.month}월 ${date.day}일';
  return date.year == now.year ? day : '${date.year}년 $day';
}

// Colors and sizes follow the SC-07 mockup of the screen plan (ver.2).
const _line = Color(0xFFDDE3E0);
const _chipFill = Color(0xFFF3F5F4);
const _buttonLine = Color(0xFFC9D0CC);
const _ink = Color(0xFF17201C);
const _subInk = Color(0xFF3D4743);
const _muted = Color(0xFF5A6560);
const _disabledInk = Color(0xFF8A948F);
const _danger = Color(0xFFB42318);

ButtonStyle _footerButton({required Color color, required Color line}) =>
    OutlinedButton.styleFrom(
      minimumSize: const Size.fromHeight(54),
      foregroundColor: color,
      backgroundColor: Colors.white,
      disabledForegroundColor: _disabledInk,
      disabledBackgroundColor: Colors.white,
      textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
    ).copyWith(
      side: WidgetStateProperty.resolveWith(
        (states) => BorderSide(
          color: states.contains(WidgetState.disabled) ? _buttonLine : line,
        ),
      ),
    );

/// What SC-07 shows for a loaded answer. The contract does not separate "no
/// records", "being built" and "stale" (asked in PR #117), so the app reads
/// them from the profile together with the latest analysis job.
enum ProfileView {
  /// A `ready` profile is shown (with a banner when an analysis is running,
  /// failed or ended unknown).
  profile,

  /// No profile and no analysis ever ran: records are needed.
  noAnalysis,

  /// No profile yet; an analysis is running.
  analyzing,

  /// The analysis failed because there was not enough to read.
  needsRecords,

  /// The records changed while the analysis ran.
  sourceChanged,

  /// The analysis failed for another reason.
  failed,

  /// The server could not tell whether the result was published.
  unknown,

  /// The last analysis succeeded but no `ready` profile is served: the
  /// records changed afterwards and the profile is outdated.
  outdated,
}

ProfileView profileViewOf(StyleProfileState state) {
  if (state.profile != null) return ProfileView.profile;
  final analysis = state.analysis;
  if (analysis == null) return ProfileView.noAnalysis;
  return switch (analysis.status) {
    AnalysisStatus.running => ProfileView.analyzing,
    AnalysisStatus.succeeded => ProfileView.outdated,
    AnalysisStatus.unknown => ProfileView.unknown,
    AnalysisStatus.failed => switch (analysis.errorCode) {
      'insufficient_input' => ProfileView.needsRecords,
      'source_changed' => ProfileView.sourceChanged,
      _ => ProfileView.failed,
    },
  };
}

/// The user may start the analysis again (PR #117: only after a failure, an
/// unknown result or a conflict; the server starts the first one itself).
bool canRequestAnalysis(StyleProfileState state) {
  final analysis = state.analysis;
  if (analysis == null || analysis.running) return false;
  return analysis.retryable || state.profile == null;
}

/// SC-07 말투 프로필.
///
/// Shows the speech profile the server answered, the analysis state, and the
/// form of address the partner uses for the user (`provider_address`). The
/// rename and re-analyze buttons stay pinned below the scrolling content, as
/// in the screen plan.
class SpeechProfileScreen extends StatefulWidget {
  const SpeechProfileScreen({
    super.key,
    required this.personas,
    required this.personaId,
    this.onOpenRecordUpload,
  });
  final PersonaController personas;
  final String personaId;
  final RecordUploadLauncher? onOpenRecordUpload;

  @override
  State<SpeechProfileScreen> createState() => _SpeechProfileScreenState();
}

class _SpeechProfileScreenState extends State<SpeechProfileScreen> {
  late final StyleProfileController _profile = StyleProfileController(
    widget.personas.service,
    widget.personaId,
  );

  @override
  void initState() {
    super.initState();
    _profile.load();
  }

  @override
  void dispose() {
    _profile.dispose();
    super.dispose();
  }

  Persona? get _persona {
    for (final persona in widget.personas.personas) {
      if (persona.id == widget.personaId) return persona;
    }
    return null;
  }

  void _notice(String text) {
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _rename(Persona persona) async {
    final saved = await openPersonaRename(context, widget.personas, persona);
    if (saved != null && mounted) _notice('대화 상대 이름을 저장했어요.');
  }

  Future<void> _editAddress(Persona persona) async {
    final saved = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.white,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
      builder: (_) => ProviderAddressSheet(
        personas: widget.personas,
        persona: persona,
        candidates: _profile.state?.profile?.addressTerms ?? const [],
      ),
    );
    if (saved == true && mounted) _notice('나를 부르는 호칭을 저장했어요.');
  }

  Future<void> _uploadRecords(Persona persona) async {
    final launcher = widget.onOpenRecordUpload;
    if (launcher == null) {
      _notice('기록 올리기는 아직 연결되지 않았어요.');
      return;
    }
    await launcher(context, persona);
    if (mounted) await _profile.load(quiet: true);
  }

  Future<void> _requestAnalysis() async {
    try {
      await _profile.requestAnalysis();
      if (!mounted) return;
      final analysis = _profile.state?.analysis;
      if (analysis?.status == AnalysisStatus.succeeded) {
        // An unknown result turned out to be published: fetch the profile.
        _notice('분석이 이미 끝나 있었어요.');
        await _profile.load(quiet: true);
      } else {
        _notice('말투 분석을 다시 시작했어요.');
      }
    } catch (error) {
      if (!mounted) return;
      _notice(switch (error) {
        BackendFailure(code: 'INVALID_INPUT') =>
          '확정된 기록이 없어요. 기록을 올려 확정한 뒤 다시 시도해 주세요.',
        BackendFailure(code: 'NOT_FOUND') => '대화 상대를 찾을 수 없어요.',
        _ => '분석을 요청하지 못했어요. 다시 시도해 주세요.',
      });
    }
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([widget.personas, _profile]),
    builder: (context, _) {
      final persona = _persona;
      return Scaffold(
        backgroundColor: replicaBackground,
        appBar: AppBar(
          backgroundColor: replicaBackground,
          surfaceTintColor: Colors.transparent,
          title: const Text(
            '말투 프로필',
            style: TextStyle(fontWeight: FontWeight.w700),
          ),
        ),
        body: SafeArea(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 720),
              child: persona == null
                  ? const Padding(
                      padding: EdgeInsets.all(32),
                      child: Text('대화 상대를 찾을 수 없어요.'),
                    )
                  : _body(persona),
            ),
          ),
        ),
      );
    },
  );

  Widget _body(Persona persona) {
    final state = _profile.state;
    final status = _profile.status;
    final ready = state != null && status == LoadStatus.ready;
    final view = ready ? profileViewOf(state) : null;
    return Column(
      children: [
        Expanded(
          child: RefreshIndicator(
            onRefresh: () => _profile.load(quiet: true),
            child: ListView(
              physics: const AlwaysScrollableScrollPhysics(),
              padding: const EdgeInsets.fromLTRB(20, 4, 20, 20),
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(4, 4, 4, 6),
                  child: Text(
                    '${persona.label}의 말투',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontSize: 28,
                      fontWeight: FontWeight.w700,
                      height: 1.25,
                    ),
                  ),
                ),
                if (state?.profile case final profile? when ready)
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 4),
                    child: Text(
                      '${profileDate(profile.updatedAt, DateTime.now())} 갱신',
                      style: const TextStyle(color: _muted, fontSize: 13),
                    ),
                  ),
                const SizedBox(height: 14),
                _AddressCard(
                  persona: persona,
                  onEdit: () => _editAddress(persona),
                ),
                const SizedBox(height: 14),
                if (!ready)
                  status == LoadStatus.failed
                      ? _StateCard(
                          icon: Icons.cloud_off_outlined,
                          title: '말투 프로필을 불러오지 못했어요.',
                          action: TextButton.icon(
                            onPressed: _profile.load,
                            icon: const Icon(Icons.refresh),
                            label: const Text('다시 시도'),
                          ),
                        )
                      : const _LoadingState()
                else if (view == ProfileView.profile) ...[
                  ..._analysisBanner(state),
                  ..._sections(state.profile!),
                ] else
                  _stateCard(view!, persona),
              ],
            ),
          ),
        ),
        _footer(persona, state: ready ? state : null),
      ],
    );
  }

  Widget? _checkAgain() => _profile.watchExpired
      ? TextButton.icon(
          onPressed: _profile.checkAgain,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('상태 다시 확인'),
        )
      : null;

  /// A notice above a shown profile about the latest analysis.
  List<Widget> _analysisBanner(StyleProfileState state) {
    final analysis = state.analysis;
    final Widget banner;
    if (analysis == null || analysis.status == AnalysisStatus.succeeded) {
      return const [];
    } else if (analysis.running) {
      banner = _Banner(
        icon: Icons.autorenew,
        text: _profile.watchExpired
            ? '말투를 다시 분석하고 있어요. 아직 끝나지 않았어요. 대화는 지금 프로필로 계속돼요.'
            : '새 기록으로 말투를 다시 분석하고 있어요. 대화는 지금 프로필로 계속돼요.',
        color: replicaGreen,
        background: const Color(0xFFE3EFEB),
        action: _checkAgain(),
      );
    } else if (analysis.status == AnalysisStatus.unknown) {
      banner = const _Banner(
        icon: Icons.help_outline,
        text: '마지막 분석 결과를 확인하지 못했어요. 지금 프로필은 그대로 쓰여요. 다시 분석하면 결과부터 확인해요.',
        color: _subInk,
        background: Color(0xFFEFF2F1),
      );
    } else {
      banner = const _Banner(
        icon: Icons.error_outline,
        text: '말투를 다시 분석하지 못했어요. 지금 프로필은 그대로 쓰여요.',
        color: _danger,
        background: Color(0xFFFDECEA),
      );
    }
    return [banner, const SizedBox(height: 14)];
  }

  List<Widget> _sections(StyleProfile profile) => [
    _SectionCard(title: '자주 쓰는 표현', items: profile.expressions),
    const SizedBox(height: 14),
    _SectionCard(title: '호칭', items: profile.addressTerms),
    const SizedBox(height: 14),
    _SectionCard(title: '문장 끝맺음', items: profile.endings),
  ];

  Widget _uploadButton(Persona persona) => FilledButton.icon(
    onPressed: () => _uploadRecords(persona),
    icon: const Icon(Icons.upload_file_outlined, size: 18),
    label: const Text('기록 올리기'),
  );

  Widget _stateCard(ProfileView view, Persona persona) => switch (view) {
    ProfileView.noAnalysis => _StateCard(
      icon: Icons.folder_open_outlined,
      title: '아직 분석한 기록이 없어요',
      body: '기록을 올려 확정하면 자주 쓰는 표현·호칭·문장 끝맺음을 자동으로 분석해 여기에 보여 줘요.',
      action: _uploadButton(persona),
    ),
    ProfileView.analyzing => _StateCard(
      icon: Icons.autorenew,
      title: '말투를 분석하고 있어요',
      body: _profile.watchExpired
          ? '아직 끝나지 않았어요. 잠시 후 상태를 다시 확인해 주세요.'
          : '기록이 많으면 시간이 걸려요. 이 화면을 열어 두면 자동으로 다시 확인해요.',
      action:
          _checkAgain() ??
          const SizedBox(
            width: 24,
            height: 24,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              semanticsLabel: '말투 분석 중',
            ),
          ),
    ),
    ProfileView.needsRecords => _StateCard(
      icon: Icons.folder_open_outlined,
      title: '말투를 파악하려면 기록이 더 필요해요',
      body: '대화 상대의 말이 담긴 기록을 더 올려 확정해 주세요.',
      action: _uploadButton(persona),
    ),
    ProfileView.sourceChanged => const _StateCard(
      icon: Icons.sync_problem_outlined,
      title: '분석하는 동안 기록이 바뀌었어요',
      body: '아래 "다시 분석"으로 바뀐 기록을 분석해 주세요.',
    ),
    ProfileView.failed => const _StateCard(
      icon: Icons.error_outline,
      title: '말투를 분석하지 못했어요',
      body: '아래 "다시 분석"으로 다시 시도할 수 있어요.',
    ),
    ProfileView.unknown => const _StateCard(
      icon: Icons.help_outline,
      title: '분석 결과를 확인하지 못했어요',
      body: '아래 "다시 분석"을 누르면 결과가 있는지부터 확인해요.',
    ),
    ProfileView.outdated => const _StateCard(
      icon: Icons.update,
      title: '프로필을 새로 만들어야 해요',
      body: '기록이 바뀌어 지금 프로필을 쓸 수 없어요. 아래 "다시 분석"으로 새로 만들어 주세요.',
    ),
    ProfileView.profile => const SizedBox.shrink(),
  };

  Widget _footer(Persona persona, {required StyleProfileState? state}) {
    final canRequest = state != null && canRequestAnalysis(state);
    final requesting = _profile.requesting;
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 12, 20, 16),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          OutlinedButton.icon(
            style: _footerButton(color: _ink, line: _buttonLine),
            onPressed: () => _rename(persona),
            icon: const Icon(Icons.edit_outlined, size: 20),
            label: const Text('대화 상대 이름 수정'),
          ),
          if (canRequest || requesting) ...[
            const SizedBox(height: 10),
            OutlinedButton.icon(
              style: _footerButton(color: replicaGreen, line: replicaGreen),
              onPressed: requesting ? null : _requestAnalysis,
              icon: requesting
                  ? const SizedBox(
                      width: 18,
                      height: 18,
                      child: CircularProgressIndicator(
                        strokeWidth: 2,
                        semanticsLabel: '분석 요청 중',
                      ),
                    )
                  : const Icon(Icons.refresh, size: 20),
              label: Text(requesting ? '요청하는 중…' : '다시 분석'),
            ),
          ],
        ],
      ),
    );
  }
}

/// "나를 부르는 호칭": how the partner addresses the user.
class _AddressCard extends StatelessWidget {
  const _AddressCard({required this.persona, required this.onEdit});
  final Persona persona;
  final VoidCallback onEdit;

  @override
  Widget build(BuildContext context) {
    final address = persona.providerAddress;
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 12, 8, 12),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: _line),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  '나를 부르는 호칭',
                  style: TextStyle(
                    fontSize: 14,
                    fontWeight: FontWeight.w600,
                    color: _subInk,
                  ),
                ),
                const SizedBox(height: 6),
                Text(
                  address ?? '아직 정하지 않았어요',
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: 16,
                    color: address == null ? _muted : _ink,
                    fontWeight: address == null
                        ? FontWeight.w400
                        : FontWeight.w600,
                  ),
                ),
              ],
            ),
          ),
          TextButton(
            onPressed: onEdit,
            child: Text(address == null ? '정하기' : '수정'),
          ),
        ],
      ),
    );
  }
}

/// Edits `provider_address`. An empty field clears it. The profile's
/// `address_terms` are offered as suggestions only; nothing is filled in
/// without the user picking it. Pops `true` once saved.
class ProviderAddressSheet extends StatefulWidget {
  const ProviderAddressSheet({
    super.key,
    required this.personas,
    required this.persona,
    this.candidates = const [],
  });
  final PersonaController personas;
  final Persona persona;
  final List<String> candidates;

  @override
  State<ProviderAddressSheet> createState() => _ProviderAddressSheetState();
}

class _ProviderAddressSheetState extends State<ProviderAddressSheet> {
  late final _text = TextEditingController(
    text: widget.persona.providerAddress ?? '',
  );
  bool _saving = false;
  String? _error;

  /// Trimmed input; null when empty (clears the address).
  String? get _value {
    final text = _text.text.trim();
    return text.isEmpty ? null : text;
  }

  bool get _changed => _value != widget.persona.providerAddress;

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  void _pick(String term) {
    _text.value = TextEditingValue(
      text: term,
      selection: TextSelection.collapsed(offset: term.length),
    );
    setState(() => _error = null);
  }

  Future<void> _save() async {
    if (_saving || !_changed) return;
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      await widget.personas.update(
        widget.persona.id,
        PersonaUpdate.providerAddress(_value),
      );
      if (mounted) Navigator.of(context).pop(true);
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        _error = error is BackendFailure && error.code == 'INVALID_INPUT'
            ? '입력한 내용을 확인해 주세요.'
            : '저장하지 못했어요. 입력한 호칭은 그대로 두었어요. 다시 시도해 주세요.';
      });
    }
  }

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: !_saving,
    child: Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
      child: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(24, 24, 24, 16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                '나를 부르는 호칭',
                style: TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 8),
              Text(
                '${widget.persona.label}이(가) 나를 부르던 말을 적어 주세요. 대화에서 이 호칭으로 불러요. 비워 두고 저장하면 지워져요.',
                style: const TextStyle(color: _subInk, height: 1.5),
              ),
              const SizedBox(height: 16),
              Semantics(
                label: '나를 부르는 호칭',
                child: TextField(
                  controller: _text,
                  enabled: !_saving,
                  autofocus: true,
                  maxLength: personaNameMaxLength,
                  maxLengthEnforcement: MaxLengthEnforcement.enforced,
                  textInputAction: TextInputAction.done,
                  onChanged: (_) => setState(() => _error = null),
                  onSubmitted: (_) => _save(),
                  decoration: InputDecoration(
                    hintText: '예: 우리 딸, 민지야',
                    errorText: _error,
                    errorMaxLines: 3,
                    filled: true,
                    fillColor: Colors.white,
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                  ),
                ),
              ),
              if (widget.candidates.isNotEmpty) ...[
                const SizedBox(height: 4),
                const Text(
                  '기록에서 찾은 호칭',
                  style: TextStyle(fontSize: 13, color: _muted),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final term in widget.candidates)
                      ActionChip(
                        label: Text(term),
                        onPressed: _saving ? null : () => _pick(term),
                      ),
                  ],
                ),
              ],
              const SizedBox(height: 20),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton(
                      onPressed: _saving
                          ? null
                          : () => Navigator.of(context).pop(false),
                      child: const Text('취소'),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: FilledButton(
                      onPressed: _saving || !_changed ? null : _save,
                      child: _saving
                          ? const SizedBox(
                              width: 18,
                              height: 18,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                semanticsLabel: '저장 중',
                              ),
                            )
                          : const Text('저장'),
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    ),
  );
}

class _SectionCard extends StatelessWidget {
  const _SectionCard({required this.title, required this.items});
  final String title;
  final List<String> items;

  @override
  Widget build(BuildContext context) => Container(
    width: double.infinity,
    padding: const EdgeInsets.all(16),
    decoration: BoxDecoration(
      color: Colors.white,
      borderRadius: BorderRadius.circular(16),
      border: Border.all(color: _line),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          title,
          style: const TextStyle(
            fontSize: 14,
            fontWeight: FontWeight.w600,
            color: _subInk,
          ),
        ),
        const SizedBox(height: 12),
        if (items.isEmpty)
          const Text(
            '추출된 항목이 없어요',
            style: TextStyle(color: _muted, fontSize: 13),
          )
        else
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [for (final item in items) _ItemChip(item)],
          ),
      ],
    ),
  );
}

class _ItemChip extends StatelessWidget {
  const _ItemChip(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
    decoration: BoxDecoration(
      color: _chipFill,
      borderRadius: BorderRadius.circular(18),
      border: Border.all(color: _line),
    ),
    child: Text(
      text,
      style: const TextStyle(fontSize: 14, height: 1.4, color: _ink),
    ),
  );
}

class _LoadingState extends StatelessWidget {
  const _LoadingState();

  @override
  Widget build(BuildContext context) => const Padding(
    padding: EdgeInsets.symmetric(vertical: 48),
    child: Center(
      child: CircularProgressIndicator(semanticsLabel: '말투 프로필 불러오는 중'),
    ),
  );
}

class _StateCard extends StatelessWidget {
  const _StateCard({
    required this.icon,
    required this.title,
    this.body,
    this.action,
  });
  final IconData icon;
  final String title;
  final String? body;
  final Widget? action;

  @override
  Widget build(BuildContext context) => Container(
    width: double.infinity,
    padding: const EdgeInsets.all(24),
    decoration: BoxDecoration(
      color: Colors.white,
      borderRadius: BorderRadius.circular(16),
      border: Border.all(color: _line),
    ),
    child: Column(
      children: [
        Icon(icon, size: 40, color: replicaGreen),
        const SizedBox(height: 16),
        Text(
          title,
          textAlign: TextAlign.center,
          style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
        ),
        if (body != null) ...[
          const SizedBox(height: 8),
          Text(
            body!,
            textAlign: TextAlign.center,
            style: const TextStyle(color: _muted, height: 1.5),
          ),
        ],
        if (action != null) ...[const SizedBox(height: 20), action!],
      ],
    ),
  );
}

class _Banner extends StatelessWidget {
  const _Banner({
    required this.icon,
    required this.text,
    required this.color,
    required this.background,
    this.action,
  });
  final IconData icon;
  final String text;
  final Color color, background;
  final Widget? action;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(14),
    decoration: BoxDecoration(
      color: background,
      borderRadius: BorderRadius.circular(14),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(icon, size: 18, color: color),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                text,
                style: TextStyle(color: color, fontSize: 13, height: 1.5),
              ),
            ),
          ],
        ),
        if (action != null)
          Align(alignment: Alignment.centerRight, child: action),
      ],
    ),
  );
}
