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

  group('display name and provider address (PR #117 proposal)', () {
    test('the object reads both fields; unset or blank means null', () {
      final full = Persona.fromJson({
        ...personaJson('p', '김영희'),
        'display_name': ' 엄마 ',
        'provider_address': '우리 딸',
      });
      expect(full.displayName, '엄마');
      expect(full.providerAddress, '우리 딸');
      expect(full.dataRevision, 1);
      expect(full.label, '엄마');

      final bare = Persona.fromJson({
        ...personaJson('p', '김영희'),
        'display_name': null,
        'provider_address': '  ',
      });
      expect(bare.displayName, isNull);
      expect(bare.providerAddress, isNull);
      expect(bare.label, '김영희');
      expect(Persona.fromJson({'id': 'p', 'name': '김영희'}).dataRevision, isNull);

      expect(
        () => Persona.fromJson({...personaJson('p', 'a'), 'display_name': 3}),
        throwsFormatException,
      );
      expect(
        () =>
            Persona.fromJson({...personaJson('p', 'a'), 'data_revision': '1'}),
        throwsFormatException,
      );
    });

    test('an update sends only what changes', () {
      expect(PersonaUpdate(displayName: ' 엄마 ').toJson(), {
        'display_name': '엄마',
      });
      expect(PersonaUpdate.providerAddress(' 우리 딸 ').toJson(), {
        'provider_address': '우리 딸',
      });
      expect(PersonaUpdate.providerAddress('  ').toJson(), {
        'provider_address': null,
      });
      expect(PersonaUpdate.providerAddress(null, displayName: '엄마').toJson(), {
        'display_name': '엄마',
        'provider_address': null,
      });
      expect(PersonaUpdate().isEmpty, isTrue);
      expect(() => PersonaUpdate(displayName: '  '), throwsArgumentError);
      expect(
        () => PersonaUpdate(displayName: '가' * (personaNameMaxLength + 1)),
        throwsArgumentError,
      );
      expect(
        () => PersonaUpdate.providerAddress('가' * (personaNameMaxLength + 1)),
        throwsArgumentError,
      );
    });

    test('HTTP update is a PATCH with the update as its whole body', () async {
      final (service, requests) = await httpService(
        (request) async => jsonResponse({
          ...personaJson('p1', '김영희'),
          'display_name': '엄마',
          'provider_address': null,
        }),
      );
      final persona = await service.update(
        'p1',
        PersonaUpdate.providerAddress(null, displayName: '엄마'),
      );
      expect(persona.label, '엄마');
      expect(persona.name, '김영희');
      expect(requests, hasLength(1));
      expect(requests.single.method, 'PATCH');
      expect(requests.single.url.path, '/personas/p1');
      expect(
        requests.single.headers['Authorization'],
        'Bearer synthetic-token',
      );
      expect(jsonDecode(requests.single.body), {
        'display_name': '엄마',
        'provider_address': null,
      });
    });

    test('HTTP update rejects an empty update without a request', () async {
      final (service, requests) = await httpService(
        (request) async => jsonResponse(personaJson('p1', '엄마')),
      );
      await expectLater(
        service.update('p1', PersonaUpdate()),
        throwsArgumentError,
      );
      expect(requests, isEmpty);
    });

    test('HTTP update rejects an answer about another partner', () async {
      final (service, _) = await httpService(
        (request) async => jsonResponse(personaJson('other', '엄마')),
      );
      await expectLater(
        service.update('p1', PersonaUpdate(displayName: '엄마')),
        throwsFormatException,
      );
    });

    test('HTTP update passes a refusal on unchanged', () async {
      final (service, requests) = await httpService(
        (request) async => jsonResponse({
          'error': {'code': 'INVALID_INPUT', 'retryable': false},
        }, 422),
      );
      await expectLater(
        service.update('p1', PersonaUpdate(displayName: '엄마')),
        throwsA(
          isA<BackendFailure>().having((f) => f.code, 'code', 'INVALID_INPUT'),
        ),
      );
      expect(requests, hasLength(1));
    });

    test('the dummy account keeps updates and the creation name', () async {
      final service = MemoryPersonaService(
        personas: const [Persona(id: 'p1', name: '김영희')],
      );
      await service.update('p1', PersonaUpdate(displayName: '엄마'));
      await service.update('p1', PersonaUpdate.providerAddress('우리 딸'));
      var stored = (await service.list()).single;
      expect(stored.name, '김영희');
      expect(stored.label, '엄마');
      expect(stored.providerAddress, '우리 딸');

      // Changing the display name leaves the provider address alone.
      await service.update('p1', PersonaUpdate(displayName: '어머니'));
      stored = (await service.list()).single;
      expect(stored.providerAddress, '우리 딸');
      await service.update('p1', PersonaUpdate.providerAddress(null));
      expect((await service.list()).single.providerAddress, isNull);

      await expectLater(
        service.update('missing', PersonaUpdate(displayName: '엄마')),
        throwsA(isA<BackendFailure>()),
      );
    });

    test('conversation screens show the display name', () {
      final persona = ConversationPersona.fromJson({
        ...personaJson('p1', '김영희'),
        'display_name': '엄마',
      });
      expect(persona.name, '엄마');
      expect(
        ConversationPersona.fromJson({
          ...personaJson('p1', '김영희'),
          'display_name': ' ',
        }).name,
        '김영희',
      );
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

    test(
      'an update replaces the partner in place and keeps the selection',
      () async {
        final service = FakePersonaService(
          personas: const [
            Persona(id: 'p1', name: '김영희'),
            Persona(id: 'p2', name: '박철수'),
          ],
        );
        final controller = controllerFor(service);
        await controller.load();
        controller.select('p2');
        var notified = 0;
        controller.addListener(() => notified++);
        await controller.update('p1', PersonaUpdate(displayName: '엄마'));
        expect(controller.personas.map((p) => p.label), ['엄마', '박철수']);
        expect(controller.current!.id, 'p2');
        expect(notified, 1);
      },
    );

    test('a failed update changes nothing', () async {
      final service = FakePersonaService(
        personas: const [Persona(id: 'p1', name: '김영희')],
      )..updateError = StateError('synthetic failure');
      final controller = controllerFor(service);
      await controller.load();
      await expectLater(
        controller.update('p1', PersonaUpdate(displayName: '엄마')),
        throwsStateError,
      );
      expect(controller.current!.label, '김영희');
    });

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
