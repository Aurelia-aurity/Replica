/// How long the app waits for each kind of backend request.
///
/// A screen-level `.timeout` and the HTTP adapter's timeout wrap the same call,
/// so both must use the same constant; otherwise the shorter one ends the wait.
library;

/// Text answers have no speech step, so the backend's AI request limit
/// (`LLM_TIMEOUT_SECONDS`, not agreed yet) is what bounds them. The app waits
/// the same 3 minutes as for speech and will see the backend's error earlier.
const textRequestTimeout = Duration(seconds: 180);

/// TTS requests. The backend retries a failed synthesis inside one request and
/// stops at the team's agreed app wait of 3 minutes (minus a 10 second margin).
const speechRequestTimeout = Duration(seconds: 180);

/// The backend's limit starts when the request arrives, so uploading the
/// recording is extra time on top of [speechRequestTimeout].
const voiceUploadAllowance = Duration(seconds: 20);

/// Voice turns run STT, the AI and TTS in one request, then return the audio.
/// This is [speechRequestTimeout] + [voiceUploadAllowance] (200 seconds).
const voiceRequestTimeout = Duration(seconds: 200);

/// Failure code for a speech or voice request that outlasted the app's wait.
const speechTimeoutCode = 'SPEECH_TIMEOUT';

/// Raised locally, without a request, when the previous speech or voice request
/// for the same turn may still be running on the backend. A second request
/// then would make the backend close the running stage as expired.
const speechInProgressCode = 'SPEECH_IN_PROGRESS';
