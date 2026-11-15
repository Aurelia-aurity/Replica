import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'backend_service.dart';

/// Authenticated JSON transport shared by the HTTP service adapters.
///
/// Every request obtains fresh authorization headers, runs inside
/// [BackendService] so a 401 or an account change is handled in one place,
/// times out after [timeout] (30 seconds unless the caller says otherwise), and
/// never retries on its own. Raw error bodies are reduced to [BackendFailure];
/// messages from the server are not exposed. A timeout or a dropped connection
/// becomes a retryable network failure carrying [timeoutCode] or
/// [unavailableCode], so callers can tell an unanswered request from a refusal.
class ApiClient {
  ApiClient({
    required this.backend,
    required this.baseUrl,
    required this.client,
  });
  final BackendService backend;
  final Uri baseUrl;
  final http.Client client;

  Uri uri(String path, [Map<String, String>? query]) => baseUrl.replace(
    path: '${baseUrl.path.replaceFirst(RegExp(r'/$'), '')}$path',
    queryParameters: query,
  );

  Future<Object?> request(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, Object?>? body,
    Duration timeout = defaultTimeout,
    String? timeoutCode,
    String? unavailableCode,
  }) {
    final request = http.Request(method, uri(path, query))
      ..headers['Content-Type'] = 'application/json';
    if (body != null) request.body = jsonEncode(body);
    return perform(
      request,
      timeout: timeout,
      timeoutCode: timeoutCode,
      unavailableCode: unavailableCode,
    );
  }

  static const defaultTimeout = Duration(seconds: 30);

  Future<Object?> perform(
    http.BaseRequest request, {
    Duration timeout = defaultTimeout,
    String? timeoutCode,
    String? unavailableCode,
  }) => backend.run(() async {
    final headers = await backend.auth.authorizationHeaders();
    // Check generation again after asynchronous token acquisition.
    return backend.run(() async {
      request.headers.addAll(headers);
      late http.Response response;
      try {
        response = await (() async => http.Response.fromStream(
          await client.send(request),
        ))().timeout(timeout);
      } on TimeoutException {
        throw BackendFailure(
          BackendFailureKind.network,
          code: timeoutCode,
          retryable: true,
        );
      } on http.ClientException {
        throw BackendFailure(
          BackendFailureKind.network,
          code: unavailableCode,
          retryable: true,
        );
      }
      Object? json;
      if (response.bodyBytes.isNotEmpty) {
        try {
          json = jsonDecode(utf8.decode(response.bodyBytes));
        } catch (_) {
          if (response.statusCode >= 200 && response.statusCode < 300) rethrow;
        }
      }
      if (response.statusCode < 200 || response.statusCode >= 300) {
        throw BackendFailure.fromResponse(
          statusCode: response.statusCode,
          body: json,
        );
      }
      if (request.method == 'DELETE' && response.statusCode != 204) {
        throw const FormatException('Deletion not confirmed');
      }
      return json;
    });
  });
}
