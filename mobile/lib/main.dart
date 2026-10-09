import 'package:flutter/material.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

import 'auth/auth_config.dart';
import 'auth/auth_gate.dart';
import 'auth/auth_service.dart';
import 'auth/preview_auth_service.dart';
import 'auth/supabase_auth_service.dart';
import 'chat/chat_service.dart';
import 'config/app_config.dart';
import 'shared/conversation_widgets.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final appConfig = AppConfig.fromEnvironment();
  final config = AuthConfig.fromEnvironment(appConfig.environment);
  AuthService auth = UnconfiguredAuthService();
  if (config.mode == 'preview') {
    auth = PreviewAuthService();
  } else if (config.isConfigured) {
    try {
      await Supabase.initialize(
        url: config.url.toString(),
        publishableKey: config.publishableKey!,
        authOptions: const FlutterAuthClientOptions(
          authFlowType: AuthFlowType.implicit,
          persistSession: false,
          detectSessionInUri: false,
        ),
        debug: false,
      );
      auth = SupabaseAuthService(Supabase.instance.client);
    } catch (_) {
      auth.notice = '인증 서비스를 시작하지 못했어요. 앱을 다시 실행해 주세요.';
    }
  }
  runApp(ReplicaApp(authService: auth));
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
