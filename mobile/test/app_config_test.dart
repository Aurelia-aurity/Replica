import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/config/app_config.dart';

void main() {
  test('development accepts local backend URL', () {
    final config = AppConfig.parse(
      environment: 'dev',
      backendBaseUrl: 'http://10.0.2.2:8000',
    );
    expect(config.backendBaseUrl.host, '10.0.2.2');
  });
  test('production accepts explicit HTTPS backend', () {
    final config = AppConfig.parse(
      environment: 'prod',
      backendBaseUrl: 'https://api.example.com/v1',
    );
    expect(config.environment, 'prod');
    expect(config.backendBaseUrl.path, '/v1');
  });
  test('invalid environment or base URL fails early', () {
    for (final url in [
      '',
      '/relative',
      'ftp://example.com',
      'https://user:secret@example.com',
      'https://example.com?q=1',
    ]) {
      expect(
        () => AppConfig.parse(environment: 'dev', backendBaseUrl: url),
        throwsArgumentError,
      );
    }
    expect(
      () => AppConfig.parse(
        environment: 'prod',
        backendBaseUrl: 'http://localhost:8000',
      ),
      throwsArgumentError,
    );
    expect(
      () => AppConfig.parse(
        environment: 'unknown',
        backendBaseUrl: 'https://example.com',
      ),
      throwsArgumentError,
    );
  });
}
