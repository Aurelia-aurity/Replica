import 'dart:async';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../chat/chat_service.dart';
import '../config/app_config.dart';
import '../history/conversation_service.dart';
import '../home/home_screen.dart';
import '../services/backend_service.dart';
import 'auth_screen.dart';
import 'auth_service.dart';
import 'preview_auth_service.dart';

/// Each account owns a separate navigation tree, including dialogs and drafts.
class AuthGate extends StatefulWidget {
  const AuthGate({super.key, required this.auth, required this.chat});
  final AuthService auth;
  final ChatService chat;
  @override
  State<AuthGate> createState() => _AuthGateState();
}

class _AuthGateState extends State<AuthGate> with WidgetsBindingObserver {
  var _navigator = GlobalKey<NavigatorState>();
  late int _generation;
  ConversationService? _history;
  http.Client? _historyClient;

  void _resetHistory() {
    _historyClient?.close();
    _historyClient = null;
    _history = null;
  }

  ConversationService _conversationService() {
    if (_history != null) return _history!;
    final backend = BackendService(widget.auth);
    if (widget.auth.isPreview) {
      return _history = MemoryConversationService(
        backend: backend,
        chat: widget.chat,
      );
    }
    final client = _historyClient = http.Client();
    return _history = HttpConversationService(
      backend: backend,
      baseUrl: AppConfig.fromEnvironment().backendBaseUrl,
      client: client,
    );
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _generation = widget.auth.generation;
    widget.auth.addListener(_changed);
  }

  void _changed() {
    setState(() {
      if (_generation != widget.auth.generation) {
        _resetHistory();
        _generation = widget.auth.generation;
        _navigator = GlobalKey<NavigatorState>();
      }
    });
  }

  @override
  void didUpdateWidget(AuthGate oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.auth != widget.auth) {
      _resetHistory();
      oldWidget.auth.removeListener(_changed);
      widget.auth.addListener(_changed);
      _generation = widget.auth.generation;
      _navigator = GlobalKey<NavigatorState>();
    }
  }

  @override
  void dispose() {
    _resetHistory();
    WidgetsBinding.instance.removeObserver(this);
    widget.auth.removeListener(_changed);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) widget.auth.checkSessionExpiry();
  }

  @override
  Widget build(BuildContext context) {
    if (widget.auth.identity == null) return AuthScreen(service: widget.auth);
    return ScaffoldMessenger(
      key: ValueKey(_generation),
      child: NavigatorPopHandler<Object?>(
        onPopWithResult: (_) => _navigator.currentState!.pop(),
        child: Navigator(
          key: _navigator,
          onGenerateRoute: (_) => MaterialPageRoute<void>(
            builder: (_) => HomeScreen(
              history: _conversationService(),
              accountEmail: widget.auth.identity!.email,
              onSignOut: () =>
                  unawaited(widget.auth.signOut().catchError((Object _) {})),
              onExpireSession: widget.auth is PreviewAuthService
                  ? (widget.auth as PreviewAuthService).expireSession
                  : null,
            ),
          ),
        ),
      ),
    );
  }
}
