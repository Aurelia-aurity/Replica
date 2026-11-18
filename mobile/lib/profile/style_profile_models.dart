import 'package:flutter/foundation.dart';

/// The three item groups SC-07 shows from `content.speech_style`.
enum ProfileSection { expressions, addressTerms, endings }

/// Current speech profile of one partner (`말투 프로필 객체`). Only `ready`
/// profiles are served (`GET /personas/{id}/style-profile`).
@immutable
class StyleProfile {
  const StyleProfile({
    required this.version,
    required this.updatedAt,
    this.sourceRevision,
    this.expressions = const [],
    this.addressTerms = const [],
    this.endings = const [],
  });
  final int version;
  final DateTime updatedAt;

  /// Data revision the profile was built from. Null when not sent.
  final int? sourceRevision;
  final List<String> expressions, addressTerms, endings;

  factory StyleProfile.fromJson(Map<String, dynamic> json) {
    if (json['status'] != 'ready') {
      throw const FormatException('Profile is not ready');
    }
    final content = json['content'];
    final version = json['version'];
    final revision = json['source_revision'];
    final stamp = json['updated_at'] ?? json['created_at'];
    if (content is! Map ||
        version is! int ||
        stamp is! String ||
        (revision != null && revision is! int)) {
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
      sourceRevision: revision as int?,
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
}

enum AnalysisStatus {
  running,
  succeeded,
  failed,

  /// The server could not tell whether the result was published. A new
  /// request makes it check first (PR #117).
  unknown,
}

/// State of the latest analysis job (`분석 상태 객체`, PR #117 proposal).
@immutable
class ProfileAnalysis {
  const ProfileAnalysis({
    required this.operationId,
    required this.status,
    required this.startedAt,
    required this.updatedAt,
    this.errorCode,
  });
  final String operationId;
  final AnalysisStatus status;
  final DateTime startedAt, updatedAt;

  /// Safe failure code such as `insufficient_input`, `source_changed` or
  /// `ai_failed`. Never shown raw.
  final String? errorCode;

  bool get running => status == AnalysisStatus.running;

  /// A user request may start a new analysis (failure or unknown result).
  bool get retryable =>
      status == AnalysisStatus.failed || status == AnalysisStatus.unknown;

  factory ProfileAnalysis.fromJson(Map<String, dynamic> json) {
    final id = json['operation_id'];
    final status = switch (json['status']) {
      'running' => AnalysisStatus.running,
      'succeeded' => AnalysisStatus.succeeded,
      'failed' => AnalysisStatus.failed,
      'unknown' => AnalysisStatus.unknown,
      _ => throw const FormatException('Invalid analysis status'),
    };
    final started = json['started_at'], updated = json['updated_at'];
    final code = json['error_code'];
    if (id is! String ||
        id.isEmpty ||
        started is! String ||
        updated is! String ||
        (code != null && code is! String)) {
      throw const FormatException('Invalid analysis');
    }
    return ProfileAnalysis(
      operationId: id,
      status: status,
      startedAt: DateTime.parse(started),
      updatedAt: DateTime.parse(updated),
      errorCode: code as String?,
    );
  }
}

/// `profile_state` of the style profile answer (PR #117 proposal).
enum ProfileState {
  /// No profile yet.
  none,

  /// An analysis is building the profile.
  building,

  /// A profile is served.
  ready,

  /// The records changed; the old profile is not used and must be rebuilt.
  stale,
}

/// Answer of `GET /personas/{id}/style-profile` (PR #117 proposal).
///
/// [profile] is null when there is no `ready` profile. [profileState] says
/// why (none yet, being built, or `stale`); it is null when the server does
/// not send it yet, and the app then reads the situation from [analysis].
@immutable
class StyleProfileState {
  const StyleProfileState({this.profile, this.analysis, this.profileState});
  final StyleProfile? profile;
  final ProfileAnalysis? analysis;
  final ProfileState? profileState;

  /// An analysis is under way: the server says the profile is being built,
  /// or the latest analysis job is still running.
  bool get inProgress =>
      profileState == ProfileState.building || analysis?.running == true;

  factory StyleProfileState.fromJson(Map<String, dynamic> json) {
    final profile = json['profile'], analysis = json['analysis'];
    if ((profile != null && profile is! Map<String, dynamic>) ||
        (analysis != null && analysis is! Map<String, dynamic>)) {
      throw const FormatException('Invalid style profile answer');
    }
    final state = switch (json['profile_state']) {
      null => null,
      'none' => ProfileState.none,
      'building' => ProfileState.building,
      'ready' => ProfileState.ready,
      'stale' => ProfileState.stale,
      _ => throw const FormatException('Invalid profile state'),
    };
    if (state == ProfileState.ready && profile == null) {
      throw const FormatException('Ready profile missing');
    }
    // Only a ready profile is used. With [state] given, a profile sent for any
    // other state (a `stale` one, say) is ignored rather than shown.
    final usable = state == null || state == ProfileState.ready;
    return StyleProfileState(
      profile: profile == null || !usable
          ? null
          : StyleProfile.fromJson(profile),
      analysis: analysis == null ? null : ProfileAnalysis.fromJson(analysis),
      profileState: state,
    );
  }

  StyleProfileState withAnalysis(ProfileAnalysis value) => StyleProfileState(
    profile: profile,
    analysis: value,
    profileState: profileState,
  );
}
