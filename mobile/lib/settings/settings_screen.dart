import 'package:flutter/material.dart';

import '../chat/ai_disclosure_sheet.dart';
import '../shared/conversation_widgets.dart';

const _settingsText = Color(0xFF17201C);
const _settingsMuted = Color(0xFF5A6560);
const _settingsDanger = Color(0xFFB42318);

/// SC-12 settings and account information.
class SettingsScreen extends StatelessWidget {
  const SettingsScreen({
    super.key,
    this.accountEmail,
    this.accountCreatedAt,
    required this.isDemo,
    this.onSignOut,
    this.onDeleteAccount,
    this.onExpireSession,
    this.bottomNavigationBar,
  });

  final String? accountEmail;
  final DateTime? accountCreatedAt;
  final bool isDemo;
  final Future<void> Function()? onSignOut;

  /// Starts the account deletion flow. When null (no record service that can
  /// delete this account), the row only explains that deletion is unavailable.
  final Future<void> Function()? onDeleteAccount;
  final VoidCallback? onExpireSession;
  final Widget? bottomNavigationBar;

  String get _accountDateLabel {
    final date = accountCreatedAt?.toLocal();
    if (date == null) return '가입일 정보 미제공';
    final year = date.year.toString().padLeft(4, '0');
    final month = date.month.toString().padLeft(2, '0');
    final day = date.day.toString().padLeft(2, '0');
    return '$year.$month.$day 가입';
  }

  Future<void> _showDetails(
    BuildContext context, {
    required String title,
    required Widget child,
  }) => showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    showDragHandle: true,
    backgroundColor: Colors.white,
    builder: (context) => SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.sizeOf(context).height * .8,
        ),
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(24, 0, 24, 24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                title,
                style: const TextStyle(
                  fontSize: 22,
                  fontWeight: FontWeight.w700,
                  color: _settingsText,
                ),
              ),
              const SizedBox(height: 20),
              DefaultTextStyle.merge(
                style: const TextStyle(
                  fontSize: 15,
                  height: 1.6,
                  color: _settingsMuted,
                ),
                child: child,
              ),
              const SizedBox(height: 24),
              OutlinedButton(
                onPressed: () => Navigator.of(context).pop(),
                child: const Text('닫기'),
              ),
            ],
          ),
        ),
      ),
    ),
  );

  void _consents(BuildContext context) => _showDetails(
    context,
    title: '기록 사용 동의 내역',
    child: const Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          '내 기록이 쓰이는 방식',
          style: TextStyle(fontWeight: FontWeight.w600, color: _settingsText),
        ),
        SizedBox(height: 4),
        Text('사용 범위 안내 초안', style: TextStyle(fontSize: 12)),
        SizedBox(height: 12),
        Text('말투 프로필 · 확정한 기록을 바탕으로 표현·호칭·끝맺음 등 말투 특징을 정리해요.'),
        SizedBox(height: 8),
        Text('기록 검색 · 질문과 관련된 내용을 확정한 기록에서 찾아요.'),
        SizedBox(height: 8),
        Text('응답 생성 · 찾은 기록과 말투 프로필을 참고해 AI가 응답을 만들어요. 실제 그분이 한 말은 아니에요.'),
        SizedBox(height: 12),
        Text('모델 학습에 사용할지는 아직 정해지지 않았어요.'),
        SizedBox(height: 12),
        Divider(color: Color(0xFFE6EAE8)),
        SizedBox(height: 12),
        Icon(Icons.fact_check_outlined, size: 40, color: _settingsMuted),
        SizedBox(height: 16),
        Text(
          '동의 내역을 아직 확인할 수 없어요',
          textAlign: TextAlign.center,
          style: TextStyle(fontWeight: FontWeight.w600, color: _settingsText),
        ),
        SizedBox(height: 12),
        Text('기록별 동의 내역 조회는 준비 중이에요. 연결 후 파일명과 동의 시각을 확인할 수 있어요.'),
        SizedBox(height: 12),
        Text(
          '회원가입 시 개인 기록 처리 동의와 기록별 자료 사용 동의는 별개예요. 현재 표시할 내역이 없다는 이유로 동의 여부를 판단하지 않아요.',
        ),
      ],
    ),
  );

  void _deleteAccount(BuildContext context) => _showDetails(
    context,
    title: '계정 삭제',
    child: const Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          '계정 삭제는 아직 지원하지 않아요',
          style: TextStyle(color: _settingsDanger, fontWeight: FontWeight.w600),
        ),
        SizedBox(height: 12),
        Text('삭제 확인 화면과 처리 기능이 연결되지 않았어요. 지금은 삭제 요청을 보내지 않아요.'),
        SizedBox(height: 20),
        Text(
          '삭제 범위',
          style: TextStyle(fontWeight: FontWeight.w600, color: _settingsText),
        ),
        SizedBox(height: 8),
        Text('모든 원본 기록 · 전사문 · 검색 색인 · 말투 프로필 · 대화 이력 · 계정'),
        SizedBox(height: 12),
        Text(
          '기능이 제공되면 비밀번호 재확인과 삭제 결과 확인을 거쳐 진행해요. 전체 삭제가 완료되기 전에는 자동으로 로그아웃하지 않아요.',
        ),
      ],
    ),
  );

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: replicaBackground,
    body: SafeArea(
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 720),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(20, 24, 20, 28),
            children: [
              const Padding(
                padding: EdgeInsets.fromLTRB(6, 6, 6, 22),
                child: Text(
                  '설정',
                  style: TextStyle(
                    fontSize: 26,
                    fontWeight: FontWeight.w700,
                    color: _settingsText,
                  ),
                ),
              ),
              _SettingsGroup(
                children: [
                  Padding(
                    padding: const EdgeInsets.all(16),
                    child: Row(
                      children: [
                        const CircleAvatar(
                          radius: 24,
                          backgroundColor: Color(0xFFE3EEE9),
                          child: Icon(
                            Icons.person_outline,
                            color: replicaGreen,
                          ),
                        ),
                        const SizedBox(width: 14),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                accountEmail?.trim().isNotEmpty == true
                                    ? accountEmail!
                                    : '이메일 정보 없음',
                                style: const TextStyle(
                                  fontSize: 16,
                                  fontWeight: FontWeight.w600,
                                  color: _settingsText,
                                ),
                              ),
                              const SizedBox(height: 4),
                              Text(
                                '이메일 계정 · $_accountDateLabel',
                                style: const TextStyle(
                                  fontSize: 13,
                                  color: _settingsMuted,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 22),
              _Section(
                title: '대화',
                children: [
                  _SettingsRow(
                    title: 'AI 생성 안내 다시 보기',
                    onTap: () {
                      showAiDisclosure(
                        context,
                        isDemo: isDemo,
                        reviewOnly: true,
                      );
                    },
                  ),
                  _SettingsRow(
                    title: '말투 프로필',
                    onTap: () => _showDetails(
                      context,
                      title: '말투 프로필',
                      child: const Text(
                        '말투 프로필 화면 연결은 준비 중이에요. 대화 상대와 프로필을 확인하는 기능이 연결되면 여기서 볼 수 있어요.',
                      ),
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 22),
              _Section(
                title: '개인정보',
                children: [
                  _SettingsRow(
                    title: '기록 사용 동의 내역',
                    onTap: () => _consents(context),
                  ),
                ],
              ),
              const SizedBox(height: 22),
              _Section(
                title: '계정',
                children: [
                  _SettingsRow(
                    title: '로그아웃',
                    showChevron: false,
                    onTap: () => showDialog<void>(
                      context: context,
                      builder: (_) => _SignOutDialog(onSignOut: onSignOut),
                    ),
                  ),
                  _SettingsRow(
                    title: '계정 삭제',
                    destructive: true,
                    showChevron: false,
                    onTap: onDeleteAccount ?? () => _deleteAccount(context),
                  ),
                ],
              ),
              if (onExpireSession != null) ...[
                const SizedBox(height: 16),
                TextButton(
                  onPressed: onExpireSession,
                  child: const Text('세션 만료 확인 (미리보기)'),
                ),
              ],
            ],
          ),
        ),
      ),
    ),
    bottomNavigationBar: bottomNavigationBar,
  );
}

class _Section extends StatelessWidget {
  const _Section({required this.title, required this.children});
  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      Padding(
        padding: const EdgeInsets.fromLTRB(6, 0, 6, 8),
        child: Text(
          title,
          style: const TextStyle(
            fontSize: 13,
            fontWeight: FontWeight.w600,
            color: _settingsMuted,
          ),
        ),
      ),
      _SettingsGroup(children: children),
    ],
  );
}

class _SettingsGroup extends StatelessWidget {
  const _SettingsGroup({required this.children});
  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Material(
    color: Colors.white,
    shape: RoundedRectangleBorder(
      side: const BorderSide(color: Color(0xFFDDE3E0)),
      borderRadius: BorderRadius.circular(16),
    ),
    clipBehavior: Clip.antiAlias,
    child: Column(
      children: [
        for (var i = 0; i < children.length; i++) ...[
          if (i > 0) const Divider(height: 1, color: Color(0xFFE6EAE8)),
          children[i],
        ],
      ],
    ),
  );
}

class _SettingsRow extends StatelessWidget {
  const _SettingsRow({
    required this.title,
    required this.onTap,
    this.destructive = false,
    this.showChevron = true,
  });
  final String title;
  final VoidCallback onTap;
  final bool destructive;
  final bool showChevron;

  @override
  Widget build(BuildContext context) => InkWell(
    onTap: onTap,
    child: ConstrainedBox(
      constraints: const BoxConstraints(minHeight: 52),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        child: Row(
          children: [
            Expanded(
              child: Text(
                title,
                style: TextStyle(
                  fontSize: 15,
                  color: destructive ? _settingsDanger : _settingsText,
                  fontWeight: destructive ? FontWeight.w600 : FontWeight.w400,
                ),
              ),
            ),
            if (showChevron) ...[
              const SizedBox(width: 12),
              const Icon(Icons.chevron_right, size: 18, color: _settingsMuted),
            ],
          ],
        ),
      ),
    ),
  );
}

class _SignOutDialog extends StatefulWidget {
  const _SignOutDialog({this.onSignOut});
  final Future<void> Function()? onSignOut;

  @override
  State<_SignOutDialog> createState() => _SignOutDialogState();
}

class _SignOutDialogState extends State<_SignOutDialog> {
  bool _busy = false;
  bool _failed = false;

  Future<void> _signOut() async {
    if (_busy || widget.onSignOut == null) return;
    setState(() {
      _busy = true;
      _failed = false;
    });
    try {
      await widget.onSignOut!();
      if (mounted) Navigator.of(context).pop();
    } catch (_) {
      if (mounted) {
        setState(() {
          _busy = false;
          _failed = true;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: !_busy,
    child: AlertDialog(
      title: const Text('로그아웃할까요?'),
      content: Text(
        _failed
            ? '로그아웃하지 못했어요. 다시 시도해 주세요.'
            : widget.onSignOut == null
            ? '로그아웃 기능이 아직 연결되지 않았어요.'
            : '이 계정에서 로그아웃하고 로그인 화면으로 돌아가요.',
      ),
      actions: [
        TextButton(
          onPressed: _busy ? null : () => Navigator.of(context).pop(),
          child: const Text('취소'),
        ),
        FilledButton(
          onPressed: _busy || widget.onSignOut == null ? null : _signOut,
          child: Text(_busy ? '로그아웃 중…' : '로그아웃'),
        ),
      ],
    ),
  );
}
