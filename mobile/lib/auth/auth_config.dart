class AuthConfig {
  const AuthConfig({required this.mode, this.url, this.publishableKey});
  final String mode;
  final Uri? url;
  final String? publishableKey;
  bool get isConfigured => url != null && publishableKey != null;

  factory AuthConfig.fromEnvironment(String environment) => AuthConfig.parse(
    environment: environment,
    mode: const String.fromEnvironment('AUTH_MODE', defaultValue: 'supabase'),
    url: const String.fromEnvironment('SUPABASE_URL'),
    publishableKey: const String.fromEnvironment('SUPABASE_PUBLISHABLE_KEY'),
  );

  factory AuthConfig.parse({
    required String environment,
    required String mode,
    String url = '',
    String publishableKey = '',
  }) {
    if (!['preview', 'supabase'].contains(mode) ||
        (mode == 'preview' && environment != 'dev')) {
      throw ArgumentError('AUTH_MODE preview is allowed only in dev.');
    }
    if (mode == 'preview' || (url.isEmpty && publishableKey.isEmpty)) {
      return AuthConfig(mode: mode);
    }
    final uri = Uri.tryParse(url);
    if (uri == null ||
        uri.scheme != 'https' ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.path.isNotEmpty && uri.path != '/') ||
        !publishableKey.startsWith('sb_publishable_') ||
        publishableKey == 'sb_publishable_REPLACE_ME') {
      throw ArgumentError('Set an HTTPS SUPABASE_URL and a publishable key.');
    }
    return AuthConfig(mode: mode, url: uri, publishableKey: publishableKey);
  }
}
