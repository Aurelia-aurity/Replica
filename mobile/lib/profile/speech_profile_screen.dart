import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'persona_controller.dart';
import 'persona_models.dart';
import 'persona_name_screen.dart';

/// Opens SC-04 (record upload) for [persona]. The records feature is built in
/// its own work item; until it is connected the screen shows a notice instead.
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

/// SC-07 말투 프로필.
///
/// Shows only what the server answered. A missing profile is one state because
/// the server does not say whether records are missing, the profile is being
/// built or it is outdated.
class SpeechProfileScreen extends StatefulWidget {
  const SpeechProfileScreen({
    super.key,
    required this.controller,
    this.onOpenRecordUpload,
  });
  final PersonaController controller;
  final RecordUploadLauncher? onOpenRecordUpload;

  @override
  State<SpeechProfileScreen> createState() => _SpeechProfileScreenState();
}

class _SpeechProfileScreenState extends State<SpeechProfileScreen> {
  PersonaController get _controller => widget.controller;

  @override
  void initState() {
    super.initState();
    // Not synchronously: listeners of other screens must not rebuild while
    // this route is still being built.
    Future.microtask(() {
      _controller.acknowledgeRegeneration();
      _controller.loadProfile();
    });
  }

  void _notice(String text) {
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(text)));
  }

  Future<void> _editName(Persona persona) async {
    final saved = await Navigator.of(context).push<Persona>(
      MaterialPageRoute(
        builder: (_) =>
            PersonaNameScreen(controller: _controller, editing: persona),
      ),
    );
    if (saved != null && mounted) _notice('대화 상대 이름을 저장했어요.');
  }

  Future<void> _uploadRecords(Persona persona) async {
    final launcher = widget.onOpenRecordUpload;
    if (launcher == null) {
      _notice('기록 올리기는 아직 연결되지 않았어요.');
      return;
    }
    await launcher(context, persona);
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: _controller,
    builder: (context, _) {
      final persona = _controller.current;
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
                  : ListView(
                      padding: const EdgeInsets.all(20),
                      children: _content(persona),
                    ),
            ),
          ),
        ),
      );
    },
  );

  List<Widget> _content(Persona persona) {
    final controller = _controller;
    final profile = controller.profile;
    final isReady =
        controller.profileStatus == LoadStatus.ready && profile != null;
    final isMissing =
        controller.profileStatus == LoadStatus.ready && profile == null;
    return [
      Text(
        '${persona.name}의 말투',
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
        style: const TextStyle(fontSize: 26, fontWeight: FontWeight.w700),
      ),
      if (isReady) ...[
        const SizedBox(height: 8),
        Text(
          _summary(profile),
          style: const TextStyle(color: Color(0xFF68766F), height: 1.5),
        ),
      ],
      if (controller.service.isPreview) ...[
        const SizedBox(height: 12),
        const _PreviewLabel(),
      ],
      const SizedBox(height: 20),
      if (isReady)
        ..._profileSections(profile)
      else if (isMissing)
        _MissingState(onUpload: () => _uploadRecords(persona))
      else if (controller.profileStatus == LoadStatus.failed)
        _FailedState(onRetry: controller.loadProfile)
      else
        const _LoadingState(),
      const SizedBox(height: 20),
      OutlinedButton.icon(
        onPressed: () => _editName(persona),
        icon: const Icon(Icons.edit_outlined, size: 18),
        label: const Text('대화 상대 이름 수정'),
      ),
      if (isReady) ...[const SizedBox(height: 12), ..._regeneration()],
    ];
  }

  String _summary(StyleProfile profile) {
    final date = '${profileDate(profile.updatedAt, DateTime.now())} 갱신';
    final count = profile.evidenceRecordCount;
    return count == null ? date : '확정된 기록 $count개에서 뽑았어요 · $date';
  }

  List<Widget> _profileSections(StyleProfile profile) => [
    _SectionCard(title: '자주 쓰는 표현', items: profile.expressions),
    const SizedBox(height: 12),
    _SectionCard(title: '호칭', items: profile.addressTerms),
    const SizedBox(height: 12),
    _SectionCard(title: '문장 끝맺음', items: profile.endings),
  ];

  List<Widget> _regeneration() {
    final status = _controller.regeneration;
    final running = status == RegenerationStatus.running;
    final supported = _controller.service.supportedFeatures.contains(
      PersonaFeature.regenerate,
    );
    return [
      FilledButton.icon(
        onPressed: supported && !running ? _controller.regenerate : null,
        icon: running
            ? const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  semanticsLabel: '프로필 다시 만드는 중',
                ),
              )
            : const Icon(Icons.refresh, size: 18),
        label: Text(running ? '다시 만드는 중…' : '프로필 다시 만들기'),
      ),
      const SizedBox(height: 8),
      if (!supported)
        const _Caption('서버가 아직 프로필 다시 만들기를 제공하지 않아요. (미연동)')
      else if (running)
        const _Caption('대화는 이전 프로필로 계속할 수 있어요.')
      else if (status == RegenerationStatus.succeeded)
        const _Banner(
          icon: Icons.check_circle_outline,
          text: '새 프로필로 바꿨어요.',
          color: replicaGreen,
          background: Color(0xFFE3EFEB),
        )
      else if (status == RegenerationStatus.failed)
        const _Banner(
          icon: Icons.error_outline,
          text: '프로필을 다시 만들지 못했어요. 이전 프로필은 그대로 쓰여요. 다시 시도해 주세요.',
          color: Color(0xFFB42318),
          background: Color(0xFFFDECEA),
        ),
    ];
  }
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
      borderRadius: BorderRadius.circular(18),
      border: Border.all(color: replicaBorder),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          title,
          style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w700),
        ),
        const SizedBox(height: 12),
        if (items.isEmpty)
          const Text(
            '추출된 항목이 없어요',
            style: TextStyle(color: Color(0xFF68766F), fontSize: 13),
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
      color: const Color(0xFFE3EFEB),
      borderRadius: BorderRadius.circular(18),
    ),
    child: Text(text, style: const TextStyle(fontSize: 14, height: 1.4)),
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
    required this.action,
  });
  final IconData icon;
  final String title;
  final String? body;
  final Widget action;

  @override
  Widget build(BuildContext context) => Container(
    width: double.infinity,
    padding: const EdgeInsets.all(24),
    decoration: BoxDecoration(
      color: Colors.white,
      borderRadius: BorderRadius.circular(18),
      border: Border.all(color: replicaBorder),
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
            style: const TextStyle(color: Color(0xFF68766F), height: 1.5),
          ),
        ],
        const SizedBox(height: 20),
        action,
      ],
    ),
  );
}

class _MissingState extends StatelessWidget {
  const _MissingState({required this.onUpload});
  final VoidCallback onUpload;

  @override
  Widget build(BuildContext context) => _StateCard(
    icon: Icons.folder_open_outlined,
    title: '말투를 파악하려면 기록이 더 필요해요',
    body: '확정한 기록이 쌓이면 자주 쓰는 표현·호칭·문장 끝맺음이 여기에 표시돼요. 이미 기록을 확정했다면 프로필을 만드는 중일 수 있어요.',
    action: FilledButton.icon(
      onPressed: onUpload,
      icon: const Icon(Icons.upload_file_outlined, size: 18),
      label: const Text('기록 올리기'),
    ),
  );
}

class _FailedState extends StatelessWidget {
  const _FailedState({required this.onRetry});
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => _StateCard(
    icon: Icons.cloud_off_outlined,
    title: '말투 프로필을 불러오지 못했어요.',
    action: TextButton.icon(
      onPressed: onRetry,
      icon: const Icon(Icons.refresh),
      label: const Text('다시 시도'),
    ),
  );
}

class _PreviewLabel extends StatelessWidget {
  const _PreviewLabel();

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
    decoration: BoxDecoration(
      color: const Color(0xFFE7ECEA),
      borderRadius: BorderRadius.circular(12),
    ),
    child: const Text(
      '미리보기 데이터예요. 서버에서 받은 말투 프로필이 아니에요.',
      style: TextStyle(fontSize: 12, color: Color(0xFF46544E), height: 1.5),
    ),
  );
}

class _Caption extends StatelessWidget {
  const _Caption(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Text(
    text,
    textAlign: TextAlign.center,
    style: const TextStyle(fontSize: 12, color: Color(0xFF68766F), height: 1.5),
  );
}

class _Banner extends StatelessWidget {
  const _Banner({
    required this.icon,
    required this.text,
    required this.color,
    required this.background,
  });
  final IconData icon;
  final String text;
  final Color color, background;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(14),
    decoration: BoxDecoration(
      color: background,
      borderRadius: BorderRadius.circular(14),
    ),
    child: Row(
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
  );
}
