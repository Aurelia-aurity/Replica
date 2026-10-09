class AppConfig {
  const AppConfig({required this.environment, required this.backendBaseUrl});

  final String environment;
  final Uri backendBaseUrl;

  factory AppConfig.fromEnvironment() => AppConfig.parse(
    environment: const String.fromEnvironment('APP_ENV', defaultValue: 'dev'),
    backendBaseUrl: const String.fromEnvironment(
      'BACKEND_BASE_URL',
      defaultValue: 'http://localhost:8000',
    ),
  );

  factory AppConfig.parse({
    required String environment,
    required String backendBaseUrl,
  }) {
    if (!const ['dev', 'staging', 'prod'].contains(environment)) {
      throw ArgumentError('APP_ENV must be dev, staging or prod.');
    }
    final uri = Uri.tryParse(backendBaseUrl);
    if (uri == null ||
        !const ['http', 'https'].contains(uri.scheme) ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (environment != 'dev' && uri.scheme != 'https')) {
      throw ArgumentError(
        'BACKEND_BASE_URL must be an HTTP(S) base URL; staging and prod require HTTPS.',
      );
    }
    return AppConfig(environment: environment, backendBaseUrl: uri);
  }
}
