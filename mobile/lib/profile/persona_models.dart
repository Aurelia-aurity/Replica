import 'package:flutter/widgets.dart';

/// SC-13: the name or form of address may be at most 20 characters.
const personaNameMaxLength = 20;

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

/// A conversation partner (`대화 상대 객체` of the app-backend contract).
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

  Persona renamed(String value) => Persona(id: id, name: value);
}

/// The three item groups SC-07 shows from `content.speech_style`.
enum ProfileSection { expressions, addressTerms, endings }

/// Current speech profile of one partner (`말투 프로필 객체`).
///
/// [evidenceRecordCount] stays null for server responses: the contract has no
/// field for it, and the app must not estimate it. Only the preview data and a
/// future contract field may fill it.
@immutable
class StyleProfile {
  const StyleProfile({
    required this.version,
    required this.updatedAt,
    this.expressions = const [],
    this.addressTerms = const [],
    this.endings = const [],
    this.evidenceRecordCount,
  });
  final int version;
  final DateTime updatedAt;
  final List<String> expressions, addressTerms, endings;
  final int? evidenceRecordCount;

  factory StyleProfile.fromJson(Map<String, dynamic> json) {
    // Only `ready` profiles are served. Anything else breaks the contract.
    if (json['status'] != 'ready') {
      throw const FormatException('Profile is not ready');
    }
    final content = json['content'];
    final version = json['version'];
    final stamp = json['updated_at'] ?? json['created_at'];
    if (content is! Map || version is! int || stamp is! String) {
      throw const FormatException('Invalid profile');
    }
    final style = content['speech_style'];
    List<String> items(String key) {
      final value = style is Map ? style[key] : null;
      if (value is! List) return const [];
      return [
        ...{
          for (final item in value)
            if (item is String && item.trim().isNotEmpty) item.trim(),
        },
      ];
    }

    return StyleProfile(
      version: version,
      updatedAt: DateTime.parse(stamp),
      expressions: items('frequent_expressions'),
      addressTerms: items('address_terms'),
      endings: items('sentence_endings'),
    );
  }

  List<String> itemsOf(ProfileSection section) => switch (section) {
    ProfileSection.expressions => expressions,
    ProfileSection.addressTerms => addressTerms,
    ProfileSection.endings => endings,
  };

  StyleProfile copyWith({
    int? version,
    DateTime? updatedAt,
    List<String>? expressions,
    List<String>? addressTerms,
    List<String>? endings,
  }) => StyleProfile(
    version: version ?? this.version,
    updatedAt: updatedAt ?? this.updatedAt,
    expressions: expressions ?? this.expressions,
    addressTerms: addressTerms ?? this.addressTerms,
    endings: endings ?? this.endings,
    evidenceRecordCount: evidenceRecordCount,
  );
}

/// Operations the app calls that the contract may not provide yet.
enum PersonaFeature { rename, regenerate, excludeItem }

/// The connected API has no endpoint for [feature]. Not a transient failure:
/// the UI must not offer a retry loop for it.
class PersonaFeatureUnavailable implements Exception {
  const PersonaFeatureUnavailable(this.feature);
  final PersonaFeature feature;
  @override
  String toString() => 'PersonaFeatureUnavailable($feature)';
}
