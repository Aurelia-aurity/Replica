import 'package:flutter/material.dart';

import 'auth/auth_gate.dart';
import 'auth/auth_service.dart';
import 'auth/preview_auth_service.dart';
import 'chat/chat_service.dart';
import 'config/app_config.dart';
import 'shared/conversation_widgets.dart';
import 'records/record_service.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  AppConfig.fromEnvironment();
  // Frontend-only entry point; actual integration replaces this injected dummy.
  runApp(ReplicaApp(authService: PreviewAuthService()));
}

class ReplicaApp extends StatelessWidget {
  const ReplicaApp({
    super.key,
    required this.authService,
    this.chatService,
    this.recordServiceFactory,
  });
  final RecordService Function()? recordServiceFactory;
  final AuthService authService;
  final ChatService? chatService;

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
        recordServiceFactory: recordServiceFactory,
        auth: authService,
        chat: chatService ?? const DemoChatService(),
      ),
    );
  }
}
