import 'package:flutter/material.dart';

import 'chat/chat_service.dart';
import 'config/app_config.dart';
import 'home/home_screen.dart';
import 'shared/conversation_widgets.dart';

void main() {
  AppConfig.fromEnvironment();
  runApp(const ReplicaApp());
}

class ReplicaApp extends StatelessWidget {
  const ReplicaApp({super.key, this.chatService});
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
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
          ),
        ),
        outlinedButtonTheme: OutlinedButtonThemeData(
          style: OutlinedButton.styleFrom(
            minimumSize: const Size(0, 48),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
          ),
        ),
      ),
      home: HomeScreen(service: chatService ?? const DemoChatService()),
    );
  }
}
