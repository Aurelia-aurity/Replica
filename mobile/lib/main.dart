import 'package:flutter/material.dart';

import 'auth/auth_gate.dart';
import 'auth/auth_config.dart';
import 'auth/auth_service.dart';
import 'auth/preview_auth_service.dart';
import 'chat/chat_service.dart';
import 'config/app_config.dart';
import 'profile/persona_service.dart';
import 'shared/conversation_widgets.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final app = AppConfig.fromEnvironment();
  final auth = AuthConfig.fromEnvironment(app.environment);
  runApp(ReplicaApp(authService: selectAuthService(app, auth)));
}

/// No real authentication client is activated until integration is agreed.
AuthService selectAuthService(AppConfig app, AuthConfig auth) =>
    app.environment == 'dev' && auth.mode == 'preview'
    ? PreviewAuthService()
    : UnconfiguredAuthService();

class ReplicaApp extends StatelessWidget {
  const ReplicaApp({
    super.key,
    required this.authService,
    this.chatService,
    this.personaServiceFactory,
  });
  final AuthService authService;
  final ChatService? chatService;

  /// Replaces the default partner service. For tests and previews.
  final PersonaService Function()? personaServiceFactory;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Replica',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: replicaGreen),
        scaffoldBackgroundColor: replicaBackground,
        useMaterial3: true,
        filledButtonTheme: FilledButtonThemeData(
          style: FilledButton.styleFrom(
            minimumSize: const Size(0, 48),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(14),
            ),
          ),
        ),
        outlinedButtonTheme: OutlinedButtonThemeData(
          style: OutlinedButton.styleFrom(
            minimumSize: const Size(0, 48),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(14),
            ),
          ),
        ),
      ),
      home: AuthGate(
        auth: authService,
        chat: chatService ?? const DemoChatService(),
        personaServiceFactory: personaServiceFactory,
      ),
    );
  }
}
