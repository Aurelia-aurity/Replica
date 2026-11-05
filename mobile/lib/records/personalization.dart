import 'record_models.dart';

/// What the app could learn about the persona's current style profile.
class ProfileEvidence {
  const ProfileEvidence._(this.readyAt, this.checked);

  /// The profile endpoint answered with no `ready` profile (none yet,
  /// generating, or stale — the draft API cannot tell these apart).
  const ProfileEvidence.none() : this._(null, true);

  /// A `ready` profile exists, last updated at [at].
  ProfileEvidence.ready(DateTime at) : this._(at, true);

  /// The check itself failed; nothing can be concluded.
  const ProfileEvidence.unavailable() : this._(null, false);
  final DateTime? readyAt;
  final bool checked;
}

/// How long after confirmation the app keeps saying "반영 중" before treating
/// the lack of a newer profile as a delay.
const personalizationWindow = Duration(minutes: 10);

/// Derives the reflection state of a confirmed transcript.
///
/// This is an inference, not proof: a profile published at or after the
/// confirmation was built from a snapshot taken after the confirmation, so it
/// contains the confirmed text. The draft API exposes no per-record evidence;
/// replace this once the server reports it.
ReviewProfileState derivePersonalization({
  required DateTime confirmedAt,
  required ProfileEvidence profile,
  required DateTime now,
  Duration window = personalizationWindow,
}) {
  if (!profile.checked) return ReviewProfileState.unknown;
  final readyAt = profile.readyAt;
  if (readyAt != null && !readyAt.isBefore(confirmedAt)) {
    return ReviewProfileState.reflected;
  }
  return now.difference(confirmedAt) <= window
      ? ReviewProfileState.processing
      : ReviewProfileState.delayed;
}
