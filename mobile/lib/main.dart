import 'package:flutter/material.dart';
import 'package:supabase_flutter/supabase_flutter.dart';

import 'auth/auth_gate.dart';
import 'auth/auth_config.dart';
import 'auth/auth_service.dart';
import 'auth/preview_auth_service.dart';
import 'auth/supabase_auth_service.dart';
import 'chat/chat_service.dart';
import 'config/app_config.dart';
import 'profile/persona_service.dart';
import 'shared/conversation_widgets.dart';
import 'records/record_service.dart';

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
  const ReplicaApp({
    super.key,
    required this.authService,
    this.chatService,
    this.personaServiceFactory,
    this.recordServiceFactory,
  });
  /// Replaces the default record service. Called once per partner id for
  /// that partner's records, and with null for account-wide deletion when
  /// there is no partner yet. For tests and previews.
  final RecordService Function(String? personaId)? recordServiceFactory;
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
        recordServiceFactory: recordServiceFactory,
        auth: authService,
        chat: chatService ?? const DemoChatService(),
        personaServiceFactory: personaServiceFactory,
      ),
    );
  }
}
