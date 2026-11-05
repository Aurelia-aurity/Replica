import 'package:flutter/material.dart';

import 'persona_controller.dart';
import 'persona_models.dart';
import 'persona_name_screen.dart';
import 'speech_profile_screen.dart';

/// Opens SC-07. A finished regeneration result from an earlier visit is cleared
/// so its banner is not shown again.
Future<void> openSpeechProfile(
  BuildContext context,
  PersonaController controller, {
  RecordUploadLauncher? onOpenRecordUpload,
}) {
  controller.acknowledgeRegeneration();
  return Navigator.of(context).push<void>(
    MaterialPageRoute(
      builder: (_) => SpeechProfileScreen(
        controller: controller,
        onOpenRecordUpload: onOpenRecordUpload,
      ),
    ),
  );
}

/// Tap on the SC-02 partner card.
///
/// Once a partner exists the card always leads to SC-07, even while no
/// analysis result exists yet: SC-07 shows that state, and SC-13 would create a
/// second partner. Only without any partner it opens SC-13 and, after saving,
/// SC-07.
Future<void> openPersonaEntry(
  BuildContext context,
  PersonaController controller, {
  RecordUploadLauncher? onOpenRecordUpload,
}) async {
  if (controller.current == null) {
    final saved = await Navigator.of(context).push<Persona>(
      MaterialPageRoute(
        builder: (_) => PersonaNameScreen(controller: controller),
      ),
    );
    if (saved == null || !context.mounted) return;
  }
  if (!context.mounted) return;
  await openSpeechProfile(
    context,
    controller,
    onOpenRecordUpload: onOpenRecordUpload,
  );
}
