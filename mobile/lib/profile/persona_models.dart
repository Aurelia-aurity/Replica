import 'package:flutter/widgets.dart';

/// SC-13: the name or form of address may be at most 50 characters. This
/// follows the app-backend contract for `display_name` and `provider_address`
/// (1 to 50 characters after trimming, PR #117) and the AI contract's
/// `persona.display_name`.
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

/// A conversation partner (`대화 상대 객체` of the app-backend contract).
///
/// [name] is the name given when the partner was created; it never changes.
/// [displayName] is the name or form of address the user edits later ("엄마"),
/// and [providerAddress] is how the partner addresses the user. Both are
/// `null` until set (PR #117, proposal). Show [label], not [name].
@immutable
class Persona {
  const Persona({
    required this.id,
    required this.name,
    this.displayName,
    this.providerAddress,
    this.dataRevision,
  });
  final String id;
  final String name;
  final String? displayName;
  final String? providerAddress;

  /// Revision of the partner's confirmed records. Used to tell whether a
  /// speech profile is up to date. Null when the server did not send it.
  final int? dataRevision;

  /// What the app shows for this partner: [displayName], else [name].
  String get label => displayName ?? name;

  factory Persona.fromJson(Map<String, dynamic> json) {
    final id = json['id'], name = json['name'];
    final revision = json['data_revision'];
    if (id is! String ||
        id.isEmpty ||
        name is! String ||
        name.trim().isEmpty ||
        (revision != null && revision is! int)) {
      throw const FormatException('Invalid persona');
    }
    return Persona(
      id: id,
      name: name,
      displayName: _optionalText(json, 'display_name'),
      providerAddress: _optionalText(json, 'provider_address'),
      dataRevision: revision as int?,
    );
  }

  /// A copy with [update] applied, as the server is expected to store it.
  Persona applying(PersonaUpdate update) => Persona(
    id: id,
    name: name,
    displayName: update.displayName ?? displayName,
    providerAddress: update.updatesProviderAddress
        ? update.providerAddress
        : providerAddress,
    dataRevision: dataRevision,
  );

  @override
  bool operator ==(Object other) =>
      other is Persona &&
      other.id == id &&
      other.name == name &&
      other.displayName == displayName &&
      other.providerAddress == providerAddress &&
      other.dataRevision == dataRevision;

  @override
  int get hashCode =>
      Object.hash(id, name, displayName, providerAddress, dataRevision);
}

/// A nullable text field of the partner object. A missing field, `null` or a
/// blank string all mean "not set"; any other type is rejected.
String? _optionalText(Map<String, dynamic> json, String key) {
  final value = json[key];
  if (value == null) return null;
  if (value is! String) throw FormatException('Invalid $key');
  final text = value.trim();
  return text.isEmpty ? null : text;
}

/// Body of `PATCH /personas/{persona_id}` (PR #117, proposal).
///
/// [displayName] can be changed but not cleared: the contract has no way back
/// to `null`. [providerAddress] can be set or cleared; it is only sent when
/// [updatesProviderAddress] is true, so "leave it" and "clear it" differ.
/// Values are normalized on construction; an invalid one throws
/// [ArgumentError], so screens should validate with [normalizePersonaName]
/// first.
@immutable
class PersonaUpdate {
  PersonaUpdate({String? displayName})
    : displayName = displayName == null ? null : _required(displayName),
      providerAddress = null,
      updatesProviderAddress = false;

  /// Sets [value] as the provider address, or clears it when [value] is null
  /// or blank.
  PersonaUpdate.providerAddress(String? value, {String? displayName})
    : displayName = displayName == null ? null : _required(displayName),
      providerAddress = _optional(value),
      updatesProviderAddress = true;

  final String? displayName;
  final String? providerAddress;
  final bool updatesProviderAddress;

  bool get isEmpty => displayName == null && !updatesProviderAddress;

  Map<String, Object?> toJson() => {
    if (displayName != null) 'display_name': displayName,
    if (updatesProviderAddress) 'provider_address': providerAddress,
  };

  static String _required(String raw) =>
      normalizePersonaName(raw) ??
      (throw ArgumentError.value(
        raw,
        'displayName',
        'must be 1 to 50 characters',
      ));

  static String? _optional(String? raw) {
    if (raw == null || raw.trim().isEmpty) return null;
    return normalizePersonaName(raw) ??
        (throw ArgumentError.value(
          raw,
          'providerAddress',
          'must be at most 50 characters',
        ));
  }
}
