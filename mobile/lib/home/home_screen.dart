import 'dart:async';

import 'package:flutter/material.dart';

import '../history/conversation.dart';
import '../history/conversation_service.dart';
import '../history/history_screen.dart';
import '../profile/persona_card.dart';
import '../profile/persona_controller.dart';
import '../profile/persona_flow.dart';
import '../profile/persona_models.dart';
import '../records/record_controller.dart';
import '../records/record_picker.dart';
import '../records/record_service.dart';
import '../records/record_upload_screen.dart';
import '../records/records_panel.dart';
import '../records/deletion_sheet.dart';
import '../shared/conversation_widgets.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({
    super.key,
    required this.history,
    required this.personas,
    this.accountEmail,
    this.onSignOut,
    this.onExpireSession,
    this.recordServiceFor,
    this.accountDeletedFor,
    this.recordPicker = pickRecordFile,
  });
  final ConversationService history;
  final PersonaController personas;

  /// Record service bound to one partner (`persona_id`), or to the account
  /// alone when [personaId] is null (account deletion before any partner
  /// exists). Called once per partner for this login. Preview data otherwise.
  final RecordService Function(String? personaId)? recordServiceFor;

  /// What to run once the account deletion finished, for records kept by
  /// [records]; null keeps account deletion unavailable.
  final VoidCallback? Function(RecordService records)? accountDeletedFor;

  /// File picker of SC-04, shared by the records tab and SC-07. For tests.
  final RecordPicker recordPicker;
  final String? accountEmail;
  final VoidCallback? onSignOut;
  final VoidCallback? onExpireSession;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  bool _starting = false;
  int _tab = 0;
  late Future<List<Conversation>> _recent;

  /// Record services of this login, one per partner id ([_accountScope] for
  /// the account alone), so switching back to a partner keeps its records.
  final _recordServices = <String, RecordService>{};
  static const _accountScope = '#account';

  RecordController? _recordController;
  String? _recordPersonaId;
  VoidCallback? _accountDeleted;

  @override
  void initState() {
    super.initState();
    _refreshRecent();
    // Not synchronously: the listeners below must not rebuild during mount.
    Future.microtask(widget.personas.load);
  }

  @override
  void dispose() {
    _recordController?.dispose();
    super.dispose();
  }

  bool get _personasLoading =>
      widget.personas.status == LoadStatus.idle ||
      widget.personas.status == LoadStatus.loading;

  /// Records of the current partner, or of the account alone while there is
  /// none. Records belong to one partner (`persona_id` of the contract), so a
  /// different partner gets its own controller and service: a list answer
  /// for one partner can never show up under another.
  RecordController get _records {
    final personaId = widget.personas.current?.id;
    final current = _recordController;
    if (current != null && _recordPersonaId == personaId) return current;
    current?.dispose();
    final service = _recordServices.putIfAbsent(
      personaId ?? _accountScope,
      () => widget.recordServiceFor?.call(personaId) ?? PreviewRecordService(),
    );
    _accountDeleted = widget.accountDeletedFor?.call(service);
    _recordPersonaId = personaId;
    final controller = RecordController(
      service,
      onAccountDeleted: _accountDeleted,
    );
    // The account scope holds no list of its own.
    if (personaId != null) unawaited(controller.refresh());
    return _recordController = controller;
  }

  void _refreshRecent() {
    _recent = widget.history.list();
  }

  /// SC-04 opened from SC-07 ("기록 올리기") of [persona]. The upload goes to
  /// that partner's records, the same ones its records tab shows.
  Future<void> _openRecordUpload(BuildContext context, Persona persona) {
    widget.personas.select(persona.id);
    final records = _records;
    if (_recordPersonaId != persona.id) return Future.value();
    records.beginSelection();
    return Navigator.of(context).push<void>(
      MaterialPageRoute(
        builder: (_) => RecordUploadScreen(
          controller: records,
          picker: widget.recordPicker,
        ),
      ),
    );
  }

  void _notice(String text) {
    ScaffoldMessenger.of(context).hideCurrentSnackBar();
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));
  }

  void _account() {
    // Until the partners are known it is unclear whose records would be
    // cleaned up, so account deletion waits for them.
    final records = _personasLoading ? null : _records;
    final accountDeleted = records == null ? null : _accountDeleted;
    showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(widget.accountEmail ?? '계정'),
              const SizedBox(height: 16),
              FilledButton(
                onPressed: () {
                  Navigator.of(sheetContext).pop();
                  widget.onSignOut?.call();
                },
                child: const Text('로그아웃'),
              ),
              TextButton.icon(
                onPressed: records == null || accountDeleted == null
                    ? null
                    : () async {
                        Navigator.of(sheetContext).pop();
                        final deletion = records.deletionFor(
                          '#account',
                          account: true,
                        );
                        final complete = await showDeletionSheet(
                          context,
                          deletion,
                          preview: records.service.isPreview,
                        );
                        if (mounted && complete == true) accountDeleted();
                      },
                icon: const Icon(Icons.delete_outline),
                label: const Text('계정 삭제'),
              ),
              if (widget.onExpireSession != null)
                TextButton(
                  onPressed: () {
                    Navigator.of(sheetContext).pop();
                    widget.onExpireSession!();
                  },
                  child: const Text('세션 만료 확인 (미리보기)'),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _start({required bool voice}) async {
    if (_starting) return;
    final persona = widget.personas.current;
    if (persona == null) {
      _notice(partnerMissingNotice);
      return;
    }
    setState(() => _starting = true);
    try {
      await startConversation(
        context,
        widget.history,
        voice: voice,
        persona: persona,
      );
    } catch (_) {
      if (mounted) _notice('새 대화를 열지 못했어요. 대화 상대와 연결 상태를 확인해 주세요.');
    } finally {
      if (mounted) {
        setState(() {
          _starting = false;
          _refreshRecent();
        });
      }
    }
  }

  Future<void> _openRecent(Conversation item) async {
    if (_starting) return;
    setState(() => _starting = true);
    try {
      final latest = await widget.history.get(item.id);
      final name = conversationTitle(latest, widget.personas.personas);
      if (name == null) {
        if (mounted) _notice(partnerMissingNotice);
        return;
      }
      if (mounted) {
        await openConversation(
          context,
          widget.history,
          latest,
          conversationName: name,
        );
      }
    } catch (_) {
      if (mounted) _notice('대화를 불러오지 못했어요. 다시 시도해 주세요.');
    } finally {
      if (mounted) {
        setState(() {
          _starting = false;
          _refreshRecent();
        });
      }
    }
  }

  Widget _navigation() => NavigationBar(
    selectedIndex: _tab,
    backgroundColor: Colors.white,
    indicatorColor: const Color(0xFFE3EFEB),
    onDestinationSelected: (index) {
      if (index < 3) {
        setState(() {
          _tab = index;
          if (index == 0) _refreshRecent();
          if (index == 1 && widget.personas.current != null) {
            _records.refresh();
          }
        });
      } else {
        _notice('설정 기능은 고도화 단계에서 제공할 예정입니다.');
      }
    },
    destinations: const [
      NavigationDestination(
        icon: Icon(Icons.home_outlined),
        selectedIcon: Icon(Icons.home),
        label: '홈',
      ),
      NavigationDestination(icon: Icon(Icons.folder_outlined), label: '기록'),
      NavigationDestination(
        icon: Icon(Icons.chat_bubble_outline),
        selectedIcon: Icon(Icons.chat_bubble),
        label: '대화',
      ),
      NavigationDestination(icon: Icon(Icons.tune), label: '설정'),
    ],
  );

  /// The records tab: the current partner's records. Records always belong to
  /// a partner, so without one the tab asks for a profile first, as the home
  /// card does.
  Widget _recordsTab() {
    final personas = widget.personas;
    final persona = personas.current;
    if (persona == null) {
      return SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(32),
            child: switch (personas.status) {
              LoadStatus.idle || LoadStatus.loading =>
                const CircularProgressIndicator(semanticsLabel: '대화 상대 불러오는 중'),
              LoadStatus.failed => TextButton.icon(
                onPressed: personas.load,
                icon: const Icon(Icons.refresh),
                label: const Text('대화 상대를 불러오지 못했어요. 다시 시도'),
              ),
              LoadStatus.ready => Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(
                    Icons.folder_open_outlined,
                    size: 40,
                    color: replicaGreen,
                  ),
                  const SizedBox(height: 16),
                  const Text(
                    '기록은 대화 상대별로 모여요',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 8),
                  const Text(
                    '먼저 대화할 상대의 이름이나 호칭을 정해 주세요.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: Color(0xFF5A6560), height: 1.5),
                  ),
                  const SizedBox(height: 20),
                  FilledButton.icon(
                    onPressed: () => createPersonaThenProfile(
                      context,
                      personas,
                      onOpenRecordUpload: _openRecordUpload,
                    ),
                    icon: const Icon(Icons.add, size: 18),
                    label: const Text('프로필 만들기'),
                  ),
                ],
              ),
            },
          ),
        ),
      );
    }
    final records = _records;
    final panel = RecordsPanel(
      // A new partner's controller needs a fresh panel (polling, filters).
      key: ObjectKey(records),
      controller: records,
      picker: widget.recordPicker,
      onAccountDeleted: _accountDeleted,
    );
    if (personas.personas.length < 2) return panel;
    // With several partners, say whose records these are.
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SafeArea(
          bottom: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 12, 20, 0),
            child: Text(
              '${persona.label}의 기록',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                color: Color(0xFF5A6560),
                fontSize: 13,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ),
        Expanded(
          child: MediaQuery.removePadding(
            context: context,
            removeTop: true,
            child: panel,
          ),
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.personas,
    builder: (context, _) => _buildBody(context),
  );

  Widget _buildBody(BuildContext context) {
    if (_tab == 1) {
      return Scaffold(
        backgroundColor: replicaBackground,
        body: _recordsTab(),
        bottomNavigationBar: _navigation(),
      );
    }
    if (_tab == 2) {
      return HistoryScreen(
        service: widget.history,
        selectedPersona: widget.personas.current,
        partners: widget.personas.personas,
        bottomNavigationBar: _navigation(),
      );
    }
    return Scaffold(
      backgroundColor: replicaBackground,
      appBar: AppBar(
        backgroundColor: replicaBackground,
        surfaceTintColor: Colors.transparent,
        title: const Text(
          '레플리카',
          style: TextStyle(fontWeight: FontWeight.w700),
        ),
        actions: [
          IconButton(
            tooltip: '계정',
            icon: const Icon(Icons.person_outline),
            onPressed: widget.onSignOut == null ? null : _account,
          ),
        ],
      ),
      body: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 720),
          child: ListView(
            padding: const EdgeInsets.all(20),
            children: [
              PersonaCard(
                controller: widget.personas,
                busy: _starting,
                onCreate: () => createPersonaThenProfile(
                  context,
                  widget.personas,
                  onOpenRecordUpload: _openRecordUpload,
                ),
                onOpenProfile: () {
                  final persona = widget.personas.current;
                  if (persona != null) {
                    openSpeechProfile(
                      context,
                      widget.personas,
                      persona,
                      onOpenRecordUpload: _openRecordUpload,
                    );
                  }
                },
                onStartText: () => _start(voice: false),
                onStartVoice: () => _start(voice: true),
              ),
              const SizedBox(height: 28),
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  const Text(
                    '최근 대화',
                    style: TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
                  ),
                  TextButton(
                    onPressed: () => setState(() => _tab = 2),
                    child: const Text('전체 보기'),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              FutureBuilder<List<Conversation>>(
                future: _recent,
                builder: (context, snapshot) {
                  if (snapshot.hasError) {
                    return TextButton.icon(
                      onPressed: () => setState(_refreshRecent),
                      icon: const Icon(Icons.refresh),
                      label: const Text('최근 대화를 불러오지 못했어요. 다시 시도'),
                    );
                  }
                  if (!snapshot.hasData) {
                    return const Center(child: CircularProgressIndicator());
                  }
                  final items = [...snapshot.data!]
                    ..sort((a, b) => b.lastAt.compareTo(a.lastAt));
                  if (items.isEmpty) {
                    return const Padding(
                      padding: EdgeInsets.all(20),
                      child: Text(
                        '아직 나눈 대화가 없어요',
                        style: TextStyle(color: Color(0xFF68766F)),
                      ),
                    );
                  }
                  return Column(
                    children: [
                      for (final item in items.take(2))
                        Card(
                          color: Colors.white,
                          elevation: 0,
                          child: ListTile(
                            onTap: _starting ? null : () => _openRecent(item),
                            title: Text(
                              item.firstQuestion,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                            ),
                            subtitle: Text(
                              '${conversationTime(item.lastAt)} · ${item.lastVoice ? '음성' : '텍스트'}',
                            ),
                            trailing: const Icon(Icons.chevron_right),
                          ),
                        ),
                    ],
                  );
                },
              ),
              const SizedBox(height: 20),
              if (widget.history.isDemo)
                const Text(
                  '데모 모드 · 메시지는 서버로 전송되지 않습니다.',
                  style: TextStyle(fontSize: 12, color: Color(0xFF68766F)),
                ),
            ],
          ),
        ),
      ),
      bottomNavigationBar: _navigation(),
    );
  }
}
