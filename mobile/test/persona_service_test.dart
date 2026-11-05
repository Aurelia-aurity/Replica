import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mobile/auth/preview_auth_service.dart';
import 'package:mobile/history/conversation_service.dart';
import 'package:mobile/profile/persona_controller.dart';
import 'package:mobile/profile/persona_models.dart';
import 'package:mobile/profile/persona_service.dart';
import 'package:mobile/profile/speech_profile_screen.dart';
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

    test('creates with the chosen id and sends no user id', () async {
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

    test(
      'profile null means none; a profile maps the three item groups',
      () async {
        var body = 'null';
        final (service, requests) = await httpService(
          (request) async => http.Response.bytes(utf8.encode(body), 200),
        );
        expect(await service.profile('p1'), isNull);
        expect(requests.single.url.path, '/personas/p1/style-profile');

        body = jsonEncode(profileJson());
        final profile = (await service.profile('p1'))!;
        expect(profile.version, 2);
        expect(profile.expressions, ['밥은 챙겨 먹었니?']);
        expect(profile.addressTerms, ['너']);
        expect(profile.endings, ['~니?', '~지.']);
        expect(profile.updatedAt, DateTime.utc(2026, 10, 7));
        // The contract has no record count, so none is shown.
        expect(profile.evidenceRecordCount, isNull);
      },
    );

    test('a missing route is a failed lookup, not "no profile"', () async {
      final (service, _) = await httpService(
        (request) async => jsonResponse({'detail': 'Not Found'}, 404),
      );
      await expectLater(service.profile('p1'), throwsA(isA<BackendFailure>()));
    });

    test('operations the contract lacks are reported, never faked', () async {
      final (service, requests) = await httpService(
        (request) async => jsonResponse({}),
      );
      expect(service.isPreview, false);
      expect(service.supportedFeatures, isEmpty);
      await expectLater(
        service.rename('p1', '이름'),
        throwsA(isA<PersonaFeatureUnavailable>()),
      );
      await expectLater(
        service.regenerate('p1'),
        throwsA(isA<PersonaFeatureUnavailable>()),
      );
      await expectLater(
        service.excludeItem('p1', ProfileSection.endings, '~나?'),
        throwsA(isA<PersonaFeatureUnavailable>()),
      );
      expect(requests, isEmpty);
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
    Future<(HttpConversationService, List<http.Request>)> conversations(
      String? Function()? selected,
    ) async {
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
        return jsonResponse({
          'id': 's1',
          'persona_id': jsonDecode(request.body)['persona_id'],
          'title': null,
          'created_at': '2026-11-01T00:00:00Z',
          'updated_at': '2026-11-01T00:00:00Z',
          'ai_notice_ack_at': '2026-11-01T00:00:00Z',
        }, 201);
      });
      addTearDown(client.close);
      return (
        HttpConversationService(
          backend: BackendService(auth),
          baseUrl: Uri.parse('http://localhost:8000'),
          client: client,
          selectedPersonaId: selected,
        ),
        requests,
      );
    }

    test(
      'a new conversation uses the partner shown on the home card',
      () async {
        final (service, requests) = await conversations(() => 'shown');
        final conversation = await service.create(DateTime.now());
        expect(conversation.personaId, 'shown');
        expect(requests.map((r) => r.url.path), ['/sessions']);
      },
    );

    test('without a selection the first active partner is used', () async {
      final (service, requests) = await conversations(() => null);
      final conversation = await service.create(DateTime.now());
      expect(conversation.personaId, 'newest');
      expect(requests.map((r) => r.url.path), ['/personas', '/sessions']);
    });
  });

  group('MemoryPersonaService (preview data)', () {
    test('is marked as preview and supports every operation', () async {
      final service = MemoryPersonaService.preview();
      expect(service.isPreview, true);
      expect(service.supportedFeatures, PersonaFeature.values.toSet());
      expect((await service.list()).single.name, '엄마');
      expect((await service.profile('preview'))!.evidenceRecordCount, 11);
    });

    test('create is idempotent per id and rejects a reused id', () async {
      final service = MemoryPersonaService();
      final first = await service.create('엄마', personaId: 'a');
      expect(await service.create('엄마', personaId: 'a'), first);
      expect(await service.list(), hasLength(1));
      await expectLater(service.create('다른', personaId: 'a'), throwsStateError);
    });

    test('rename keeps the profile; exclusion and regeneration work', () async {
      final service = MemoryPersonaService(
        regenerateDelay: Duration.zero,
        personas: const [Persona(id: 'a', name: '엄마')],
        profiles: {'a': sampleProfile()},
      );
      expect((await service.rename('a', '어머니')).name, '어머니');
      expect((await service.profile('a'))!.expressions, contains('걱정이다'));
      final excluded = await service.excludeItem(
        'a',
        ProfileSection.expressions,
        '걱정이다',
      );
      expect(excluded.expressions, ['밥은 먹었나']);
      expect(excluded.endings, ['~나?', '~데이']);
      expect((await service.regenerate('a')).version, 2);
    });
  });

  group('StyleProfile.fromJson', () {
    test(
      'drops blanks and duplicates and tolerates a missing speech_style',
      () {
        final profile = StyleProfile.fromJson(
          profileJson(
            content: {
              'schema_version': 1,
              'speech_style': {
                'frequent_expressions': ['a', ' a ', '', '  ', 3, 'b'],
              },
            },
          ),
        );
        expect(profile.expressions, ['a', 'b']);
        expect(profile.addressTerms, isEmpty);
        expect(profile.endings, isEmpty);
        expect(
          StyleProfile.fromJson(profileJson(content: {'schema_version': 1}))
              .expressions,
          isEmpty,
        );
      },
    );

    test('rejects objects that break the contract', () {
      expect(
        () => StyleProfile.fromJson(profileJson(status: 'stale')),
        throwsFormatException,
      );
      expect(
        () => StyleProfile.fromJson(profileJson(content: 'text')),
        throwsFormatException,
      );
      expect(
        () => Persona.fromJson({'id': 'x', 'name': '  '}),
        throwsFormatException,
      );
    });
  });

  test('profile dates add the year only when it differs', () {
    final now = DateTime(2026, 11, 6);
    expect(profileDate(DateTime(2026, 10, 6, 12), now), '10월 6일');
    expect(profileDate(DateTime(2025, 3, 4, 12), now), '2025년 3월 4일');
  });

  group('PersonaController', () {
    PersonaController controllerFor(FakePersonaService service) {
      final controller = PersonaController(service);
      addTearDown(controller.dispose);
      return controller;
    }

    test('loads the partner and its profile', () async {
      final service = FakePersonaService(
        personas: const [Persona(id: 'p1', name: '엄마')],
        profileValue: sampleProfile(),
      );
      final controller = controllerFor(service);
      await controller.load();
      expect(controller.personasStatus, LoadStatus.ready);
      expect(controller.current!.name, '엄마');
      expect(controller.profileStatus, LoadStatus.ready);
      expect(controller.profile, isNotNull);
    });

    test('no partner leaves the profile unloaded', () async {
      final service = FakePersonaService();
      final controller = controllerFor(service);
      await controller.load();
      expect(controller.current, isNull);
      expect(controller.profileStatus, LoadStatus.idle);
      expect(service.profileCalls, 0);
    });

    test('a failed list is a failed state that can be retried', () async {
      final service = FakePersonaService(
        personas: const [Persona(id: 'p1', name: '엄마')],
      )..listError = StateError('offline');
      final controller = controllerFor(service);
      await controller.load();
      expect(controller.personasStatus, LoadStatus.failed);
      service.listError = null;
      await controller.load();
      expect(controller.personasStatus, LoadStatus.ready);
      expect(controller.current!.name, '엄마');
    });

    test('a null profile and a failed lookup stay distinct', () async {
      final service = FakePersonaService(
        personas: const [Persona(id: 'p1', name: '엄마')],
      );
      final controller = controllerFor(service);
      await controller.load();
      expect(controller.profileStatus, LoadStatus.ready);
      expect(controller.profile, isNull);
      service.profileError = StateError('offline');
      await controller.loadProfile();
      expect(controller.profileStatus, LoadStatus.failed);
    });

    test(
      'creating keeps the partner and clears the previous profile state',
      () async {
        final service = FakePersonaService();
        final controller = controllerFor(service);
        await controller.load();
        final persona = await controller.create('엄마', personaId: 'p-new');
        expect(controller.current, persona);
        expect(controller.profileStatus, LoadStatus.idle);
      },
    );

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

    test('a late profile answer for an older request is ignored', () async {
      final service = FakePersonaService(
        personas: const [Persona(id: 'p1', name: '엄마')],
        profileValue: sampleProfile(expressions: const ['오래된']),
      );
      final controller = controllerFor(service);
      await controller.load();
      // The first lookup is answered with old data but arrives last.
      final gate = service.profileGate = Completer<void>();
      final slow = controller.loadProfile();
      service.profileGate = null;
      service.profileValue = sampleProfile(expressions: const ['최신']);
      await controller.loadProfile();
      expect(controller.profile!.expressions, ['최신']);
      gate.complete();
      await slow;
      expect(controller.profile!.expressions, ['최신']);
    });

    test(
      'regeneration keeps the previous profile while running and after failure',
      () async {
        final service = FakePersonaService(
          personas: const [Persona(id: 'p1', name: '엄마')],
          profileValue: sampleProfile(expressions: const ['이전']),
        );
        final controller = controllerFor(service);
        await controller.load();
        service.regenerateGate = Completer<void>();
        service.regenerateError = StateError('model busy');
        final running = controller.regenerate();
        expect(controller.regeneration, RegenerationStatus.running);
        await controller.regenerate(); // ignored: already running
        expect(service.regenerateCalls, 1);
        expect(controller.profile!.expressions, ['이전']);
        service.regenerateGate!.complete();
        await running;
        expect(controller.regeneration, RegenerationStatus.failed);
        expect(controller.profile!.expressions, ['이전']);

        service
          ..regenerateGate = null
          ..regenerateError = null;
        await controller.regenerate();
        expect(controller.regeneration, RegenerationStatus.succeeded);
        expect(controller.profile!.expressions, ['새 표현']);
        controller.acknowledgeRegeneration();
        expect(controller.regeneration, RegenerationStatus.idle);
      },
    );

    test('answers arriving after dispose are dropped silently', () async {
      final service = FakePersonaService(
        personas: const [Persona(id: 'p1', name: '엄마')],
        profileValue: sampleProfile(),
      )..profileGate = Completer<void>();
      final controller = PersonaController(service);
      final loading = controller.load();
      await Future<void>.delayed(Duration.zero);
      controller.dispose();
      service.profileGate!.complete();
      await loading; // would throw if it notified a disposed controller
    });
  });
}
