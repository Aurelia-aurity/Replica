import 'record_models.dart';

/// Persona-level availability exposed by the current profile endpoint.
/// None includes an absent, generating or stale profile.
enum ProfileEvidence { ready, none, unavailable }

/// A ready profile establishes availability, not whether this record was used
/// or its search index is ready. Record reflection requires backend evidence
/// for both the published profile and index of the confirmed text version.
ReviewProfileState derivePersonalization(ProfileEvidence profile) =>
    profile == ProfileEvidence.ready
    ? ReviewProfileState.available
    : ReviewProfileState.unknown;
