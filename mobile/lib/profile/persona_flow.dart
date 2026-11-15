import 'package:flutter/material.dart';

import 'persona_controller.dart';
import 'persona_models.dart';
import 'persona_name_screen.dart';

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
/// "이름 수정" (not wired yet: SC-07 is restored next).
Future<Persona?> openPersonaRename(
  BuildContext context,
  PersonaController controller,
  Persona persona,
) => Navigator.of(context).push<Persona>(
  MaterialPageRoute(
    builder: (_) => PersonaNameScreen(controller: controller, editing: persona),
  ),
);
