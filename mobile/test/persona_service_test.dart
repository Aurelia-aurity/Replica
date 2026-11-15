import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/chat/chat_service.dart';
import 'package:mobile/history/conversation.dart';
import 'package:mobile/history/conversation_service.dart';
import 'package:mobile/profile/persona_controller.dart';
import 'package:mobile/profile/persona_models.dart';
import 'package:mobile/profile/persona_service.dart';
import 'package:mobile/services/api_client.dart';
import 'package:mobile/services/backend_service.dart';

import 'persona_test_support.dart';

Future<(HttpPersonaService, List<http.Request>)> httpService(
  Future<http.Response> Function(http.Request request) handler,
) async {
  final auth = SyntheticAuth();
  addTearDown(auth.dispose);
  await auth.signIn(
    PreviewAuthService.emails.first,
    PreviewAuthService.password,
  );
  final requests = <http.Request>[];
  final client = MockClient((request) {
    requests.add(request);
    return handler(request);
  });
  addTearDown(client.close);
  final service = HttpPersonaService(
    ApiClient(
      backend: BackendService(auth),
      baseUrl: Uri.parse('http://localhost:8000'),
      client: client,
    ),
  );
  return (service, requests);
}

void main() {
  group('HttpPersonaService', () {
    test(
      'lists only active partners page by page with the auth header',
      () async {
        final (service, requests) = await httpService((request) async {
          final offset = request.url.queryParameters['offset'];
          return jsonResponse({
            'items': offset == '0'
                ? [
                    personaJson('p2', '할머니'),
                    personaJson('gone', '삭제 중', status: 'deleting'),
                  ]
                : [personaJson('p1', '엄마')],
            'limit': 2,
            'offset': int.parse(offset!),
          });
        });
        final personas = await service.list();
        expect(personas.map((p) => p.name), ['할머니', '엄마']);
        expect(requests.map((r) => r.url.queryParameters['offset']), [
          '0',
          '2',
        ]);
        for (final request in requests) {
          expect(request.method, 'GET');
          expect(request.url.path, '/personas');
          expect(request.headers['Authorization'], 'Bearer synthetic-token');
          expect(request.url.queryParameters.containsKey('user_id'), false);
        }
      },
    );

    test('creating sends the name and the app-made id, nothing else', () async {
      final (service, requests) = await httpService(
        (request) async => jsonResponse(personaJson('p-new', '엄마'), 201),
      );
      final persona = await service.create('엄마', personaId: 'p-new');
      expect(persona.id, 'p-new');
      expect(requests.single.method, 'POST');
      expect(requests.single.url.path, '/personas');
      expect(jsonDecode(requests.single.body), {
        'name': '엄마',
        'persona_id': 'p-new',
      });
      expect(
        requests.single.headers['Authorization'],
        'Bearer synthetic-token',
      );
    });

    test('a 50 character name is sent whole', () async {
      final name = '가' * 50;
      final (service, requests) = await httpService(
        (request) async => jsonResponse(personaJson('p-new', name), 201),
      );
      await service.create(name, personaId: 'p-new');
      expect(jsonDecode(requests.single.body)['name'], name);
    });

    for (final lost in <String, Future<http.Response> Function()>{
      'connection error': () async => throw http.ClientException('dropped'),
      'server error': () async => jsonResponse({
        'error': {'code': 'INTERNAL_ERROR'},
      }, 500),
      'conflict on a retry': () async => jsonResponse({
        'error': {'code': 'CONFLICT'},
      }, 409),
    }.entries) {
      test(
        'after a ${lost.key} it finds the stored partner instead of failing',
        () async {
          final (service, requests) = await httpService((request) async {
            if (request.method == 'POST') return lost.value();
            return jsonResponse(personaJson('p-new', '엄마'));
          });
          final persona = await service.create('엄마', personaId: 'p-new');
          expect(persona.name, '엄마');
          expect(requests.map((r) => r.method), ['POST', 'GET']);
          expect(requests.last.url.path, '/personas/p-new');
        },
      );
    }

    test('an unconfirmed write is reported as a failure', () async {
      final (service, _) = await httpService((request) async {
        if (request.method == 'POST') return jsonResponse({}, 500);
        return jsonResponse({'detail': 'Not Found'}, 404);
      });
      await expectLater(
        service.create('엄마', personaId: 'p-new'),
        throwsA(isA<BackendFailure>()),
      );
    });

    test(
      'a stored partner with another name is not mistaken for success',
      () async {
        final (service, _) = await httpService((request) async {
          if (request.method == 'POST') return jsonResponse({}, 500);
          return jsonResponse(personaJson('p-new', '다른 이름'));
        });
        await expectLater(
          service.create('엄마', personaId: 'p-new'),
          throwsA(isA<BackendFailure>()),
        );
      },
    );

    test('rejected input is not looked up again', () async {
      final (service, requests) = await httpService(
        (request) async => jsonResponse({
          'error': {'code': 'INVALID_INPUT', 'field': 'name'},
        }, 422),
      );
      await expectLater(
        service.create('엄마', personaId: 'p-new'),
        throwsA(
          isA<BackendFailure>().having((e) => e.code, 'code', 'INVALID_INPUT'),
        ),
      );
      expect(requests, hasLength(1));
    });

    test('a 401 ends the session through the shared backend service', () async {
      final auth = SyntheticAuth();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      final client = MockClient(
        (_) async => jsonResponse({
          'error': {'code': 'UNAUTHENTICATED'},
        }, 401),
      );
      final service = HttpPersonaService(
        ApiClient(
          backend: BackendService(auth),
          baseUrl: Uri.parse('http://localhost:8000'),
          client: client,
        ),
      );
      await expectLater(service.list(), throwsA(isA<BackendFailure>()));
      expect(auth.identity, isNull);
    });
  });

  group('conversation target', () {
    Future<(HttpConversationService, List<http.Request>)>
    conversations() async {
      final auth = SyntheticAuth();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      final requests = <http.Request>[];
      final client = MockClient((request) async {
        requests.add(request);
        if (request.url.path == '/personas') {
          return jsonResponse({
            'items': [personaJson('newest', '새 상대')],
            'limit': 100,
            'offset': 0,
          });
        }
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        return jsonResponse({
          'id': 's1',
          'persona_id': body['persona_id'],
          'title': null,
          'created_at': '2026-11-01T00:00:00Z',
          'updated_at': '2026-11-01T00:00:00Z',
          'ai_notice_ack_at': body['ai_notice_ack_at'],
        }, 201);
      });
      addTearDown(client.close);
      return (
        HttpConversationService(
          backend: BackendService(auth),
          baseUrl: Uri.parse('http://localhost:8000'),
          client: client,
        ),
        requests,
      );
    }

    test(
      'a new conversation uses the partner picked on the home card',
      () async {
        final (service, requests) = await conversations();
        final conversation = await service.createForPersona(
          'newest',
          DateTime.now(),
        );
        expect(conversation.personaId, 'newest');
        expect(conversation.personaName, '새 상대');
        expect(requests.map((r) => r.url.path), ['/personas', '/sessions']);
        await expectLater(
          service.createForPersona('gone', DateTime.now()),
          throwsA(
            isA<BackendFailure>().having((f) => f.code, 'code', 'NOT_FOUND'),
          ),
        );
      },
    );

    test('without a selection the first active partner is used', () async {
      final (service, requests) = await conversations();
      final conversation = await service.create(DateTime.now());
      expect(conversation.personaId, 'newest');
      // create() picks the first partner, then createForPersona() checks that
      // it is still active, so the list is read twice.
      expect(requests.map((r) => r.url.path), [
        '/personas',
        '/personas',
        '/sessions',
      ]);
    });
  });

  group('MemoryPersonaService (dummy account data)', () {
    test('create is idempotent per id and rejects a reused id', () async {
      final service = MemoryPersonaService();
      final first = await service.create('엄마', personaId: 'a');
      expect(await service.create('엄마', personaId: 'a'), first);
      expect(await service.list(), hasLength(1));
      await expectLater(service.create('다른', personaId: 'a'), throwsStateError);
    });

    test('the newest partner comes first', () async {
      final service = MemoryPersonaService(
        personas: const [Persona(id: 'old', name: '예전')],
      );
      await service.create('새', personaId: 'new');
      expect((await service.list()).map((p) => p.id), ['new', 'old']);
    });

    test('it starts empty: nothing is built in', () async {
      expect(await MemoryPersonaService().list(), isEmpty);
    });

    test('dummy conversations belong to the partner that was named', () async {
      final auth = SyntheticAuth();
      addTearDown(auth.dispose);
      await auth.signIn(
        PreviewAuthService.emails.first,
        PreviewAuthService.password,
      );
      final conversations = MemoryConversationService(
        backend: BackendService(auth),
        chat: const DemoChatService(),
        personaSource: () async => const [
          ConversationPersona(id: 'named', name: '정한 이름'),
        ],
      );
      final created = await conversations.create(DateTime.now());
      expect(created.personaId, 'named');
      expect(created.personaName, '정한 이름');
    });
  });

  test('persona objects need an id and a non-blank name', () {
    expect(Persona.fromJson(personaJson('p', '엄마')).name, '엄마');
    expect(
      () => Persona.fromJson({'id': 'x', 'name': '  '}),
      throwsFormatException,
    );
    expect(() => Persona.fromJson({'name': '엄마'}), throwsFormatException);
  });

  group('PersonaController', () {
    PersonaController controllerFor(FakePersonaService service) {
      final controller = PersonaController(service);
      addTearDown(controller.dispose);
      return controller;
    }

    test('loading asks only for the partner list', () async {
      final service = FakePersonaService(
        personas: const [Persona(id: 'p1', name: '엄마')],
      );
      final controller = controllerFor(service);
      await controller.load();
      expect(controller.status, LoadStatus.ready);
      expect(controller.current!.name, '엄마');
      expect(service.listCalls, 1);
    });

    test('no partner leaves current empty', () async {
      final controller = controllerFor(FakePersonaService());
      await controller.load();
      expect(controller.status, LoadStatus.ready);
      expect(controller.current, isNull);
    });

    test('a failed list is a failed state that can be retried', () async {
      final service = FakePersonaService(
        personas: const [Persona(id: 'p1', name: '엄마')],
      )..listError = StateError('offline');
      final controller = controllerFor(service);
      await controller.load();
      expect(controller.status, LoadStatus.failed);
      service.listError = null;
      await controller.load();
      expect(controller.status, LoadStatus.ready);
      expect(controller.current!.name, '엄마');
    });

    test('creating makes the new partner current', () async {
      final service = FakePersonaService();
      final controller = controllerFor(service);
      await controller.load();
      final persona = await controller.create('엄마', personaId: 'p-new');
      expect(controller.current, persona);
      expect(controller.status, LoadStatus.ready);
    });

    test('a failed create changes nothing', () async {
      final service = FakePersonaService()..createError = StateError('offline');
      final controller = controllerFor(service);
      await controller.load();
      await expectLater(
        controller.create('엄마', personaId: 'p-new'),
        throwsStateError,
      );
      expect(controller.current, isNull);
    });

    test(
      'a list answer that arrives after a create cannot drop the partner',
      () async {
        final service = FakePersonaService();
        final controller = controllerFor(service);
        final slow = controller.load(); // answers with an empty list
        await Future<void>.delayed(Duration.zero);
        await controller.create('엄마', personaId: 'p-new');
        await slow;
        expect(controller.current!.name, '엄마');
      },
    );

    test('answers arriving after dispose are dropped silently', () async {
      final service = FakePersonaService(
        personas: const [Persona(id: 'p1', name: '엄마')],
      );
      final controller = PersonaController(service);
      final loading = controller.load();
      controller.dispose();
      await loading; // would throw if it notified a disposed controller
    });
  });
}
