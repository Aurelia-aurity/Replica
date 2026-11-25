import 'package:flutter/material.dart';

import 'persona_controller.dart';
import 'persona_models.dart';
import 'persona_name_screen.dart';
import 'speech_profile_screen.dart';

/// Opens SC-13 to name the first conversation partner. Returns the saved
/// partner, or null when the user cancelled.
Future<Persona?> openPersonaCreate(
  BuildContext context,
  PersonaController controller,
) => Navigator.of(context).push<Persona>(
  MaterialPageRoute(builder: (_) => PersonaNameScreen(controller: controller)),
);

/// Opens SC-13 in edit mode to change the display name of [persona]. Returns
/// the saved partner, or null when the user cancelled. Entered from SC-07's
/// "대화 상대 이름 수정".
Future<Persona?> openPersonaRename(
  BuildContext context,
  PersonaController controller,
  Persona persona,
) => Navigator.of(context).push<Persona>(
  MaterialPageRoute(
    builder: (_) => PersonaNameScreen(controller: controller, editing: persona),
  ),
);

/// Opens SC-07 for [persona].
Future<void> openSpeechProfile(
  BuildContext context,
  PersonaController controller,
  Persona persona, {
  RecordUploadLauncher? onOpenRecordUpload,
}) => Navigator.of(context).push<void>(
  MaterialPageRoute(
    builder: (_) => SpeechProfileScreen(
      personas: controller,
      personaId: persona.id,
      onOpenRecordUpload: onOpenRecordUpload,
    ),
  ),
);

/// SC-13 then, once a partner is saved, SC-07 (issue #94: saving moves on to
/// the speech profile). Cancelling SC-13 returns to where it was opened.
Future<void> createPersonaThenProfile(
  BuildContext context,
  PersonaController controller, {
  RecordUploadLauncher? onOpenRecordUpload,
}) async {
  final saved = await openPersonaCreate(context, controller);
  if (saved == null || !context.mounted) return;
  await openSpeechProfile(
    context,
    controller,
    saved,
    onOpenRecordUpload: onOpenRecordUpload,
  );
}
