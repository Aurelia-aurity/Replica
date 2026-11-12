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
