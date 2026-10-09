import 'package:flutter/material.dart';

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
  bool _signUp = false;
  bool _busy = false;
  bool _obscure = true;
  String? _error;
  String? _message;

  Future<void> _submit() async {
    if (_busy || !_form.currentState!.validate()) return;
    FocusScope.of(context).unfocus();
    setState(() {
      _busy = true;
      _error = null;
      _message = null;
    });
    try {
      if (_signUp) {
        final signedIn = await widget.service.signUp(
          _email.text.trim(),
          _password.text,
        );
        if (!mounted) return;
        if (!signedIn) {
          setState(() {
            _signUp = false;
            _message = widget.service.isPreview
                ? '미리보기에서는 계정을 생성하거나 이메일을 보내지 않습니다. 아래 시험 계정으로 로그인해 주세요.'
                : '회원가입 요청을 받았어요. 이메일의 인증 링크를 확인한 후 로그인해 주세요.';
          });
          _password.clear();
          _confirmation.clear();
        }
      } else {
        await widget.service.signIn(_email.text.trim(), _password.text);
      }
    } on AuthFailure catch (error) {
      if (mounted) setState(() => _error = error.message);
    } catch (_) {
      if (mounted) setState(() => _error = '요청을 완료하지 못했어요. 다시 시도해 주세요.');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    _confirmation.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('레플리카')),
    body: SafeArea(
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 440),
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: Form(
              key: _form,
              child: AutofillGroup(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    Text(
                      _signUp ? '회원가입' : '로그인',
                      style: Theme.of(context).textTheme.headlineMedium,
                    ),
                    const SizedBox(height: 8),
                    const Text('로그인 후 나의 기록과 대화에 접근할 수 있어요.'),
                    if (widget.service.isPreview) ...[
                      const SizedBox(height: 16),
                      const Text('화면 확인용 미리보기 · 실제 인증이나 서버 통신을 하지 않습니다.'),
                      const Text(
                        '계정: preview.a@example.com 또는 preview.b@example.com\n비밀번호: Replica123!',
                      ),
                    ],
                    if (!widget.service.isConfigured) ...[
                      const SizedBox(height: 16),
                      const Text(
                        '인증 환경 설정이 필요합니다. 개발 환경에서는 AUTH_MODE=preview로 화면을 확인할 수 있어요.',
                      ),
                    ],
                    const SizedBox(height: 24),
                    TextFormField(
                      key: const ValueKey('auth-email'),
                      controller: _email,
                      enabled: !_busy,
                      keyboardType: TextInputType.emailAddress,
                      autofillHints: const [AutofillHints.email],
                      textInputAction: TextInputAction.next,
                      decoration: const InputDecoration(
                        labelText: '이메일',
                        border: OutlineInputBorder(),
                      ),
                      validator: (value) {
                        final email = value?.trim() ?? '';
                        return email.length <= 254 &&
                                RegExp(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
                                    .hasMatch(email)
                            ? null
                            : '올바른 이메일을 입력해 주세요.';
                      },
                    ),
                    const SizedBox(height: 16),
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
                      onFieldSubmitted: (_) {
                        if (!_signUp) _submit();
                      },
                      decoration: InputDecoration(
                        labelText: '비밀번호',
                        border: const OutlineInputBorder(),
                        suffixIcon: IconButton(
                          tooltip: _obscure ? '비밀번호 보기' : '비밀번호 숨기기',
                          onPressed: _busy
                              ? null
                              : () => setState(() => _obscure = !_obscure),
                          icon: Icon(
                            _obscure
                                ? Icons.visibility_outlined
                                : Icons.visibility_off_outlined,
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
                      const SizedBox(height: 16),
                      TextFormField(
                        key: const ValueKey('auth-confirmation'),
                        controller: _confirmation,
                        enabled: !_busy,
                        obscureText: _obscure,
                        autocorrect: false,
                        enableSuggestions: false,
                        textInputAction: TextInputAction.done,
                        decoration: const InputDecoration(
                          labelText: '비밀번호 확인',
                          border: OutlineInputBorder(),
                        ),
                        validator: (value) =>
                            value == _password.text ? null : '비밀번호가 일치하지 않습니다.',
                        onFieldSubmitted: (_) => _submit(),
                      ),
                    ],
                    if (_error ?? widget.service.notice
                        case final String error) ...[
                      const SizedBox(height: 16),
                      Text(
                        error,
                        semanticsLabel: error,
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.error,
                        ),
                      ),
                    ],
                    if (_message != null) ...[
                      const SizedBox(height: 16),
                      Text(_message!),
                    ],
                    const SizedBox(height: 24),
                    FilledButton(
                      key: const ValueKey('auth-submit'),
                      onPressed: _busy || !widget.service.isConfigured
                          ? null
                          : _submit,
                      child: _busy
                          ? const SizedBox(
                              width: 22,
                              height: 22,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : Text(_signUp ? '가입하기' : '로그인'),
                    ),
                    TextButton(
                      onPressed: _busy
                          ? null
                          : () => setState(() {
                              _signUp = !_signUp;
                              _error = null;
                              _message = null;
                              _password.clear();
                              _confirmation.clear();
                              _form.currentState!.reset();
                            }),
                      child: Text(_signUp ? '로그인으로 돌아가기' : '이메일로 회원가입'),
                    ),
                    if (widget.service.isPreview)
                      const Text('미리보기 계정과 대화는 앱을 종료하면 초기화됩니다.'),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    ),
  );
}
