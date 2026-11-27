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
import '../settings/settings_screen.dart';
import '../shared/conversation_widgets.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({
    super.key,
    required this.history,
    required this.personas,
    this.accountEmail,
    this.accountCreatedAt,
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
  final DateTime? accountCreatedAt;
  final Future<void> Function()? onSignOut;
  final VoidCallback? onExpireSession;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  bool _starting = false;
  int _tab = 0;

  /// Counts the times the tab changed, so an answer that comes after the
  /// user left a tab can tell, even once they are back on it.
  int _tabVisit = 0;
  late Future<List<Conversation>> _recent;

  /// Record state of this login, one controller (with its own service) per
  /// partner id ([_accountScope] for the account alone). A controller is
  /// kept until the login ends, not dropped when another partner is picked:
  /// disposing it would cancel an upload still in progress and lose unsent
  /// transcript edits of that partner.
  final _recordControllers = <String, RecordController>{};
  static const _accountScope = '#account';

  /// Partner whose records the records tab last loaded.
  String? _recordsLoadedFor;

  @override
  void initState() {
    super.initState();
    _refreshRecent();
    widget.personas.addListener(_personasChanged);
    // Not synchronously: the listeners below must not rebuild during mount.
    Future.microtask(widget.personas.load);
  }

  @override
  void didUpdateWidget(HomeScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.personas != widget.personas) {
      oldWidget.personas.removeListener(_personasChanged);
      widget.personas.addListener(_personasChanged);
    }
  }

  @override
  void dispose() {
    widget.personas.removeListener(_personasChanged);
    for (final controller in _recordControllers.values) {
      controller.dispose();
    }
    super.dispose();
  }

  bool get _personasLoading =>
      widget.personas.status == LoadStatus.idle ||
      widget.personas.status == LoadStatus.loading;

  /// Record state for [personaId], or for the account alone when it is null.
  /// Records belong to one partner (`persona_id` of the contract), so each
  /// partner has its own controller and service: a list answer for one
  /// partner can never show up under another.
  ///
  /// Only builds the controller the first time; it loads nothing, so it is
  /// safe to call while building. Lists are loaded by [_loadRecords].
  RecordController _recordsFor(String? personaId) =>
      _recordControllers.putIfAbsent(personaId ?? _accountScope, () {
        final service =
            widget.recordServiceFor?.call(personaId) ?? PreviewRecordService();
        return RecordController(
          service,
          onAccountDeleted: widget.accountDeletedFor?.call(service),
        );
      });

  /// Records of the current partner, or of the account alone while there is
  /// none.
  RecordController get _records => _recordsFor(widget.personas.current?.id);

  /// Loads the current partner's records list. The account scope holds no
  /// list of its own, so nothing is loaded without a partner.
  void _loadRecords() {
    final personaId = widget.personas.current?.id;
    _recordsLoadedFor = personaId;
    if (personaId != null) unawaited(_recordsFor(personaId).refresh());
  }

  /// While the records tab is open, a newly current partner (the first load,
  /// or one created from the tab) gets its list loaded. Outside the tab the
  /// list is loaded when the tab is opened.
  void _personasChanged() {
    if (_tab != 1) return;
    if (widget.personas.current?.id == _recordsLoadedFor) return;
    _loadRecords();
  }

  void _refreshRecent() {
    _recent = widget.history.list();
  }

  /// SC-04 opened from SC-07 ("기록 올리기") of [persona]. The upload goes to
  /// that partner's records, the same ones its records tab shows.
  Future<void> _openRecordUpload(BuildContext context, Persona persona) {
    widget.personas.select(persona.id);
    // Not a partner the app holds (any more): nowhere to upload to.
    if (widget.personas.current?.id != persona.id) return Future.value();
    final records = _recordsFor(persona.id);
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

  /// Shows tab [index]. Call inside `setState`.
  void _showTab(int index) {
    if (index != _tab) _tabVisit++;
    _tab = index;
  }

  /// Whether SC-12 is still what the user sees as it was on [visit]: the
  /// settings tab was not left in between, and no dialog, sheet or screen
  /// covers it.
  bool _settingsShownSince(int visit) =>
      _tab == 3 &&
      _tabVisit == visit &&
      (ModalRoute.of(context)?.isCurrent ?? true);

  void _account() {
    setState(() => _showTab(3));
  }

  /// Whether SC-12 can start account deletion. Until the partners are known it
  /// is unclear whose records would be cleaned up, so it waits for them.
  bool get _accountDeletionOffered {
    if (_personasLoading) return widget.accountDeletedFor != null;
    // Only asks the controller; its records list is not loaded for this.
    return _records.onAccountDeleted != null;
  }

  /// Runs the records deletion flow for the whole account from SC-12.
  Future<void> _deleteAccount() async {
    if (_personasLoading) {
      _notice('대화 상대를 불러오는 중이에요. 잠시 후 다시 시도해 주세요.');
      return;
    }
    final records = _records;
    final accountDeleted = records.onAccountDeleted;
    if (accountDeleted == null) return;
    final deletion = records.deletionFor('#account', account: true);
    final complete = await showDeletionSheet(
      context,
      deletion,
      preview: records.service.isPreview,
    );
    if (mounted && complete == true) accountDeleted();
  }

  /// SC-12 "말투 프로필": SC-07 of the current partner. Without a partner it
  /// starts with SC-13, as the home card does, and SC-07 follows once the
  /// partner is saved.
  ///
  /// While the partners load (also a reload after a failure) it only asks to
  /// wait, so taps during the reload open nothing more. A reload whose
  /// answer comes once the user left SC-12 (for another tab, even if they
  /// came back) or opened something over it only updates the list.
  Future<void> _openSpeechProfile() async {
    final personas = widget.personas;
    if (personas.current == null && _personasLoading) {
      _notice('대화 상대를 불러오는 중이에요. 잠시 후 다시 시도해 주세요.');
      return;
    }
    // After a failed list it is unknown whether a partner exists: load the
    // list again rather than offer a new partner.
    if (personas.current == null && personas.status == LoadStatus.failed) {
      final visit = _tabVisit;
      await personas.load();
      if (!mounted || !_settingsShownSince(visit)) return;
      if (personas.status != LoadStatus.ready) {
        _notice('대화 상대를 불러오지 못했어요. 다시 시도해 주세요.');
        return;
      }
    }
    final persona = personas.current;
    if (persona != null) {
      await openSpeechProfile(
        context,
        personas,
        persona,
        onOpenRecordUpload: _openRecordUpload,
      );
    } else {
      await createPersonaThenProfile(
        context,
        personas,
        onOpenRecordUpload: _openRecordUpload,
      );
    }
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
    onDestinationSelected: (index) => setState(() {
      _showTab(index);
      if (index == 0) _refreshRecent();
      if (index == 1) _loadRecords();
    }),
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
      onAccountDeleted: records.onAccountDeleted,
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
    if (_tab == 3) {
      return SettingsScreen(
        accountEmail: widget.accountEmail,
        accountCreatedAt: widget.accountCreatedAt,
        isDemo: widget.history.isDemo,
        onSignOut: widget.onSignOut,
        onOpenSpeechProfile: _openSpeechProfile,
        onDeleteAccount: _accountDeletionOffered ? _deleteAccount : null,
        onExpireSession: widget.onExpireSession,
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
            onPressed: _account,
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
                    onPressed: () => setState(() => _showTab(2)),
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
