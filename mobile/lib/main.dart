import 'package:flutter/material.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

import 'auth/auth_gate.dart';
import 'auth/auth_config.dart';
import 'auth/auth_service.dart';
import 'auth/preview_auth_service.dart';
import 'auth/supabase_auth_service.dart';
import 'chat/chat_service.dart';
import 'config/app_config.dart';
import 'shared/conversation_widgets.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final app = AppConfig.fromEnvironment();
  final auth = AuthConfig.fromEnvironment(app.environment);
  runApp(ReplicaApp(authService: await selectAuthService(app, auth)));
}

Future<AuthService> selectAuthService(AppConfig app, AuthConfig auth) async {
  if (app.environment == 'dev' && auth.mode == 'preview') {
    return PreviewAuthService();
  }
  if (auth.mode != 'supabase' || !auth.isConfigured) {
    return UnconfiguredAuthService();
  }
  final supabase = await Supabase.initialize(
    url: auth.url.toString(),
    publishableKey: auth.publishableKey!,
  );
  return SupabaseAuthService(
    supabase.client,
    emailRedirectTo: auth.emailRedirectTo,
  );
}

class ReplicaApp extends StatelessWidget {
  const ReplicaApp({super.key, required this.authService, this.chatService});
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
        auth: authService,
        chat: chatService ?? const DemoChatService(),
      ),
    );
  }
}
