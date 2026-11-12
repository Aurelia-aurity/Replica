import 'package:flutter/widgets.dart';

/// SC-13: the name or form of address may be at most 50 characters. This
/// follows the upper bound the AI contract sets for `persona.display_name`
/// (1 to 50 characters); the backend itself only rejects blank names.
const personaNameMaxLength = 50;

/// Trims surrounding whitespace and validates a name typed in SC-13.
///
/// Returns null when nothing is left after trimming or when more than
/// [personaNameMaxLength] characters remain. Length is counted in user
/// perceived characters, so a Korean syllable or an emoji counts once.
String? normalizePersonaName(String raw) {
  final name = raw.trim();
  if (name.isEmpty || name.characters.length > personaNameMaxLength) {
    return null;
  }
  return name;
}

/// A conversation partner (`대화 상대 객체` of the app-backend contract). Only
/// the id and the name are used by the app.
@immutable
class Persona {
  const Persona({required this.id, required this.name});
  final String id;
  final String name;

  factory Persona.fromJson(Map<String, dynamic> json) {
    final id = json['id'], name = json['name'];
    if (id is! String || id.isEmpty || name is! String || name.trim().isEmpty) {
      throw const FormatException('Invalid persona');
    }
    return Persona(id: id, name: name);
  }
}
