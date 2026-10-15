import 'package:flutter/material.dart';

import '../shared/conversation_widgets.dart';
import 'auth_service.dart';

class AuthScreen extends StatefulWidget {
  const AuthScreen({super.key, required this.service});
  final AuthService service;
  @override
  State<AuthScreen> createState() => _AuthScreenState();
}

class _AuthScreenState extends State<AuthScreen> {
  final _form = GlobalKey<FormState>();
  final _email = TextEditingController();
  final _password = TextEditingController();
  final _confirmation = TextEditingController();
  bool _signUp = false,
      _busy = false,
      _obscure = true,
      _consent = false,
      _networkFailed = false;
  String? _error, _emailServerError, _message;

  bool get _canSubmit =>
      widget.service.isConfigured &&
      !_busy &&
      _email.text.trim().isNotEmpty &&
      _password.text.isNotEmpty &&
      (!_signUp || (_confirmation.text.isNotEmpty && _consent));

  void _switchMode() {
    if (_busy) return;
    FocusScope.of(context).unfocus();
    setState(() {
      _signUp = !_signUp;
      _error = null;
      _emailServerError = null;
      _message = null;
      _networkFailed = false;
      _consent = false;
      _obscure = true;
      _resetFormKeepingEmail();
    });
  }

  void _resetFormKeepingEmail() {
    final email = _email.text;
    _form.currentState?.reset();
    _email.text = email;
    _password.clear();
    _confirmation.clear();
  }

  Future<void> _submit() async {
    if (!_canSubmit || !_form.currentState!.validate()) return;
    FocusScope.of(context).unfocus();
    ScaffoldMessenger.of(context).hideCurrentSnackBar();
    setState(() {
      _busy = true;
      _error = null;
      _networkFailed = false;
      _message = null;
    });
    try {
      if (_signUp) {
        final signedIn = await widget.service.register(
          email: _email.text.trim(),
          password: _password.text,
          personalRecordsConsent: _consent,
        );
        if (!mounted) return;
        if (!signedIn) {
          setState(() {
            _signUp = false;
            _consent = false;
            _obscure = true;
            _message = widget.service.isPreview
                ? '회원가입 화면 확인을 마쳤어요. 실제 계정 생성 없이 미리보기 계정으로 로그인해 주세요.'
                : '회원가입 요청을 받았어요. 이메일의 인증 링크를 확인한 후 로그인해 주세요.';
            _resetFormKeepingEmail();
          });
        }
      } else {
        await widget.service.signIn(_email.text.trim(), _password.text);
      }
    } on AuthFailure catch (error) {
      if (!mounted) return;
      switch (error.kind) {
        case AuthFailureKind.network:
          setState(() => _networkFailed = true);
          ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('네트워크 연결을 확인하고 다시 시도해 주세요.')),
          );
        case AuthFailureKind.emailAlreadyRegistered:
          setState(() => _emailServerError = '이미 가입된 이메일이에요. 로그인해 주세요.');
        case AuthFailureKind.invalidCredentials:
          setState(() => _error = '이메일 또는 비밀번호가 맞지 않아요');
        case AuthFailureKind.generic:
          setState(() => _error = error.message);
      }
    } catch (_) {
      if (mounted) setState(() => _error = '요청을 완료하지 못했어요. 다시 시도해 주세요.');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  InputDecoration _decoration({String? hint, Widget? suffix}) =>
      InputDecoration(
        hintText: hint,
        hintStyle: const TextStyle(color: Color(0xFF8B9690), fontSize: 16),
        filled: true,
        fillColor: Colors.white,
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 18,
          vertical: 14,
        ),
        suffixIcon: suffix,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: replicaBorder),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: replicaBorder),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: replicaGreen, width: 1.5),
        ),
        errorMaxLines: 3,
      );

  Widget _label(String text) => Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: Text(
      text,
      style: const TextStyle(
        fontSize: 14,
        fontWeight: FontWeight.w700,
        color: Color(0xFF47544E),
      ),
    ),
  );

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    _confirmation.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: replicaBackground,
    body: SafeArea(
      child: LayoutBuilder(
        builder: (context, viewport) {
          final keyboardOpen = MediaQuery.viewInsetsOf(context).bottom > 0;
          final compact = viewport.maxHeight < 720;
          final padding = compact ? 16.0 : 24.0;
          final gapUnit = compact ? 6.0 : 12.0;
          return Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 440),
              child: SingleChildScrollView(
                keyboardDismissBehavior:
                    ScrollViewKeyboardDismissBehavior.onDrag,
                padding: EdgeInsets.all(padding),
                child: ConstrainedBox(
                  constraints: BoxConstraints(
                    minHeight: (viewport.maxHeight - padding * 2).clamp(
                      0,
                      double.infinity,
                    ),
                  ),
                  child: IntrinsicHeight(
                    child: Form(
                      key: _form,
                      child: AutofillGroup(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.stretch,
                          children: [
                            SizedBox(
                              height: keyboardOpen
                                  ? 8
                                  : compact
                                  ? 8
                                  : (viewport.maxHeight * .08).clamp(16, 64),
                            ),
                            const Text(
                              '레플리카',
                              style: TextStyle(
                                fontSize: 32,
                                fontWeight: FontWeight.w800,
                                color: Color(0xFF17251E),
                                letterSpacing: -1.5,
                              ),
                            ),
                            const SizedBox(height: 8),
                            const Text(
                              '남겨둔 기록으로 다시 나누는 대화',
                              style: TextStyle(
                                fontSize: 14,
                                height: 1.5,
                                color: Color(0xFF5C6962),
                              ),
                            ),
                            if (_signUp) ...[
                              const SizedBox(height: 20),
                              const Text(
                                '회원가입',
                                style: TextStyle(
                                  fontSize: 20,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                            ],
                            SizedBox(height: gapUnit * 2),
                            const Spacer(flex: 2),
                            _label('이메일'),
                            TextFormField(
                              key: const ValueKey('auth-email'),
                              controller: _email,
                              enabled: !_busy,
                              keyboardType: TextInputType.emailAddress,
                              autocorrect: false,
                              autofillHints: const [AutofillHints.email],
                              textInputAction: TextInputAction.next,
                              autovalidateMode:
                                  AutovalidateMode.onUserInteraction,
                              forceErrorText: _emailServerError,
                              decoration: _decoration(hint: 'user@example.com'),
                              onChanged: (_) => setState(() {
                                _emailServerError = null;
                                _error = null;
                              }),
                              validator: (value) {
                                final email = value?.trim() ?? '';
                                return email.length <= 254 &&
                                        RegExp(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
                                            .hasMatch(email)
                                    ? null
                                    : '올바른 이메일을 입력해 주세요.';
                              },
                            ),
                            if (_emailServerError != null)
                              Align(
                                alignment: Alignment.centerLeft,
                                child: TextButton(
                                  onPressed: _busy ? null : _switchMode,
                                  child: const Text('로그인으로 전환'),
                                ),
                              ),
                            const SizedBox(height: 16),
                            _label('비밀번호'),
                            TextFormField(
                              key: const ValueKey('auth-password'),
                              controller: _password,
                              enabled: !_busy,
                              obscureText: _obscure,
                              autocorrect: false,
                              enableSuggestions: false,
                              autofillHints: [
                                _signUp
                                    ? AutofillHints.newPassword
                                    : AutofillHints.password,
                              ],
                              textInputAction: _signUp
                                  ? TextInputAction.next
                                  : TextInputAction.done,
                              autovalidateMode:
                                  AutovalidateMode.onUserInteraction,
                              onChanged: (_) => setState(() => _error = null),
                              onFieldSubmitted: (_) {
                                if (!_signUp) _submit();
                              },
                              decoration: _decoration(
                                suffix: IconButton(
                                  tooltip: _obscure ? '비밀번호 보기' : '비밀번호 숨기기',
                                  onPressed: _busy
                                      ? null
                                      : () => setState(
                                          () => _obscure = !_obscure,
                                        ),
                                  icon: Icon(
                                    _obscure
                                        ? Icons.visibility_outlined
                                        : Icons.visibility_off_outlined,
                                    color: const Color(0xFF68766F),
                                  ),
                                ),
                              ),
                              validator: (value) {
                                if (value == null || value.isEmpty) {
                                  return '비밀번호를 입력해 주세요.';
                                }
                                if (_signUp && value.length < 8) {
                                  return '비밀번호는 8자 이상 입력해 주세요.';
                                }
                                return null;
                              },
                            ),
                            if (_signUp) ...[
                              const SizedBox(height: 22),
                              _label('비밀번호 확인'),
                              TextFormField(
                                key: const ValueKey('auth-confirmation'),
                                controller: _confirmation,
                                enabled: !_busy,
                                obscureText: _obscure,
                                autocorrect: false,
                                enableSuggestions: false,
                                textInputAction: TextInputAction.done,
                                autovalidateMode:
                                    AutovalidateMode.onUserInteraction,
                                decoration: _decoration(),
                                onChanged: (_) => setState(() {}),
                                validator: (value) => value == _password.text
                                    ? null
                                    : '비밀번호가 일치하지 않습니다.',
                                onFieldSubmitted: (_) => _submit(),
                              ),
                              const SizedBox(height: 16),
                              CheckboxListTile(
                                key: const ValueKey('auth-consent'),
                                value: _consent,
                                onChanged: _busy
                                    ? null
                                    : (value) => setState(
                                        () => _consent = value ?? false,
                                      ),
                                controlAffinity:
                                    ListTileControlAffinity.leading,
                                contentPadding: EdgeInsets.zero,
                                activeColor: replicaGreen,
                                title: const Text(
                                  '개인 기록 처리에 동의합니다. (필수)',
                                  style: TextStyle(fontSize: 14),
                                ),
                              ),
                            ],
                            if (_error ?? widget.service.notice
                                case final String error) ...[
                              const SizedBox(height: 18),
                              Semantics(
                                liveRegion: true,
                                child: Text(
                                  error,
                                  style: TextStyle(
                                    color: Theme.of(context).colorScheme.error,
                                    height: 1.5,
                                  ),
                                ),
                              ),
                            ],
                            if (_message != null) ...[
                              const SizedBox(height: 18),
                              Text(
                                _message!,
                                style: const TextStyle(height: 1.5),
                              ),
                            ],
                            if (_networkFailed) ...[
                              const SizedBox(height: 18),
                              OutlinedButton.icon(
                                key: const ValueKey('auth-retry'),
                                onPressed: _canSubmit ? _submit : null,
                                icon: const Icon(Icons.refresh),
                                label: const Text('다시 시도'),
                              ),
                            ],
                            if (!widget.service.isConfigured) ...[
                              const SizedBox(height: 18),
                              const Text('인증 서비스 연결을 준비 중이에요.'),
                            ],
                            const SizedBox(height: 20),
                            FilledButton(
                              key: const ValueKey('auth-submit'),
                              onPressed: _canSubmit ? _submit : null,
                              style: FilledButton.styleFrom(
                                backgroundColor: replicaGreen,
                                foregroundColor: Colors.white,
                                minimumSize: const Size.fromHeight(52),
                                shape: RoundedRectangleBorder(
                                  borderRadius: BorderRadius.circular(14),
                                ),
                              ),
                              child: _busy
                                  ? const SizedBox(
                                      width: 22,
                                      height: 22,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                        color: Colors.white,
                                      ),
                                    )
                                  : Text(
                                      _signUp ? '회원가입' : '로그인',
                                      style: const TextStyle(
                                        fontSize: 18,
                                        fontWeight: FontWeight.w700,
                                      ),
                                    ),
                            ),
                            const SizedBox(height: 8),
                            Wrap(
                              alignment: WrapAlignment.center,
                              crossAxisAlignment: WrapCrossAlignment.center,
                              children: [
                                Text(
                                  _signUp ? '이미 계정이 있으신가요?' : '처음이신가요?',
                                  style: const TextStyle(
                                    color: Color(0xFF5C6962),
                                  ),
                                ),
                                TextButton(
                                  key: const ValueKey('auth-switch'),
                                  onPressed: _busy ? null : _switchMode,
                                  child: Text(
                                    _signUp ? '로그인' : '회원가입',
                                    style: const TextStyle(
                                      fontWeight: FontWeight.w700,
                                      decoration: TextDecoration.underline,
                                    ),
                                  ),
                                ),
                              ],
                            ),
                            SizedBox(height: gapUnit * 3),
                            const Spacer(flex: 3),
                            Padding(
                              padding: EdgeInsets.zero,
                              child: Container(
                                key: const ValueKey('auth-privacy'),
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 16,
                                  vertical: 12,
                                ),
                                decoration: BoxDecoration(
                                  color: const Color(0xFFE3EFEB),
                                  borderRadius: BorderRadius.circular(14),
                                ),
                                child: const Row(
                                  children: [
                                    Icon(
                                      Icons.lock_outline,
                                      color: replicaGreen,
                                      size: 20,
                                    ),
                                    SizedBox(width: 12),
                                    Expanded(
                                      child: Text(
                                        '올린 기록과 대화는 내 계정에서만 볼 수 있어요.',
                                        style: TextStyle(
                                          fontSize: 12,
                                          height: 1.5,
                                          color: Color(0xFF475E53),
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ),
          );
        },
      ),
    ),
  );
}
