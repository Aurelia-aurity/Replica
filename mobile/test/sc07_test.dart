import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mobile/profile/persona_controller.dart';
import 'package:mobile/profile/persona_models.dart';
import 'package:mobile/profile/persona_name_screen.dart';
import 'package:mobile/profile/speech_profile_screen.dart';
import 'package:mobile/profile/style_profile_controller.dart';
import 'package:mobile/profile/style_profile_models.dart';
import 'package:mobile/services/backend_service.dart';

import 'persona_service_test.dart' show httpService;
import 'persona_test_support.dart';

const partner = Persona(id: 'p1', name: '김영희', displayName: '엄마');

Map<String, dynamic> profileJson({String status = 'ready'}) => {
  'id': 'profile-1',
  'persona_id': 'p1',
  'version': 3,
  'source_revision': 5,
  'model_name': 'synthetic',
  'status': status,
  'content': {
    'speech_style': {
      'frequent_expressions': ['밥은 먹었니', ' 밥은 먹었니 ', ''],
      'address_terms': ['우리 딸'],
      'sentence_endings': ['~했니?'],
    },
  },
  'created_at': '2026-10-01T00:00:00Z',
  'updated_at': '2026-10-06T00:00:00Z',
};

Map<String, dynamic> analysisJson(String status, {String? code}) => {
  'operation_id': 'op-9',
  'status': status,
  'error_code': code,
  'started_at': '2026-11-10T00:00:00Z',
  'updated_at': '2026-11-10T00:01:00Z',
};

class Harness {
  Harness(this.service, this.personas);
  final FakePersonaService service;
  final PersonaController personas;
}

Future<Harness> openProfile(
  WidgetTester tester, {
  StyleProfileState state = const StyleProfileState(),
  Persona persona = partner,
}) async {
  final service = FakePersonaService(personas: [persona])..profileState = state;
  final personas = PersonaController(service);
  addTearDown(personas.dispose);
  await personas.load();
  await tester.pumpWidget(
    MaterialApp(
      home: SpeechProfileScreen(personas: personas, personaId: persona.id),
    ),
  );
  // Not pumpAndSettle: a running analysis shows a spinner, and settling would
  // advance fake time far enough to use up the automatic rechecks.
  await tester.pump();
  await tester.pump();
  return Harness(service, personas);
}

Finder get reanalyze => find.widgetWithText(OutlinedButton, '다시 분석');

Finder get sheetSave => find.descendant(
  of: find.byType(ProviderAddressSheet),
  matching: find.widgetWithText(FilledButton, '저장'),
);

void main() {
  group('answer parsing', () {
    test('profile and analysis are read; only ready profiles are valid', () {
      final state = StyleProfileState.fromJson({
        'profile': profileJson(),
        'analysis': analysisJson('running'),
      });
      final profile = state.profile!;
      expect(profile.version, 3);
      expect(profile.sourceRevision, 5);
      expect(profile.expressions, ['밥은 먹었니']);
      expect(profile.addressTerms, ['우리 딸']);
      expect(profile.updatedAt, DateTime.utc(2026, 10, 6));
      expect(state.analysis!.running, isTrue);
      expect(state.analysis!.operationId, 'op-9');

      final empty = StyleProfileState.fromJson({
        'profile': null,
        'analysis': null,
      });
      expect(empty.profile, isNull);
      expect(empty.analysis, isNull);

      expect(
        () => StyleProfileState.fromJson({
          'profile': profileJson(status: 'stale'),
        }),
        throwsFormatException,
      );
      expect(
        () => StyleProfileState.fromJson({'analysis': analysisJson('done')}),
        throwsFormatException,
      );
      expect(
        () => StyleProfileState.fromJson({'profile': 'x'}),
        throwsFormatException,
      );
    });

    test('views follow the profile and the latest analysis', () {
      StyleProfileState only(ProfileAnalysis? analysis) =>
          StyleProfileState(analysis: analysis);
      expect(
        profileViewOf(StyleProfileState(profile: sampleProfile())),
        ProfileView.profile,
      );
      expect(profileViewOf(only(null)), ProfileView.noAnalysis);
      expect(
        profileViewOf(only(analysisOf(AnalysisStatus.running))),
        ProfileView.analyzing,
      );
      expect(
        profileViewOf(only(analysisOf(AnalysisStatus.succeeded))),
        ProfileView.outdated,
      );
      expect(
        profileViewOf(only(analysisOf(AnalysisStatus.unknown))),
        ProfileView.unknown,
      );
      expect(
        profileViewOf(
          only(analysisOf(AnalysisStatus.failed, 'op', 'insufficient_input')),
        ),
        ProfileView.needsRecords,
      );
      expect(
        profileViewOf(
          only(analysisOf(AnalysisStatus.failed, 'op', 'source_changed')),
        ),
        ProfileView.sourceChanged,
      );
      expect(
        profileViewOf(only(analysisOf(AnalysisStatus.failed, 'op', 'x'))),
        ProfileView.failed,
      );
    });

    test('profile_state is read and checked against the profile', () {
      final ready = StyleProfileState.fromJson({
        'profile': profileJson(),
        'profile_state': 'ready',
        'analysis': null,
      });
      expect(ready.profileState, ProfileState.ready);
      expect(ready.profile, isNotNull);

      // A stale profile sent with `stale` is ignored, not an error.
      final stale = StyleProfileState.fromJson({
        'profile': profileJson(status: 'stale'),
        'profile_state': 'stale',
        'analysis': analysisJson('succeeded'),
      });
      expect(stale.profileState, ProfileState.stale);
      expect(stale.profile, isNull);

      final building = StyleProfileState.fromJson({
        'profile': null,
        'profile_state': 'building',
        'analysis': null,
      });
      expect(building.profileState, ProfileState.building);
      expect(building.inProgress, isTrue);

      // Not sent yet by the server: the app falls back to the analysis job.
      expect(
        StyleProfileState.fromJson({'profile': null}).profileState,
        isNull,
      );
      expect(
        () => StyleProfileState.fromJson({
          'profile': null,
          'profile_state': 'ready',
        }),
        throwsFormatException,
      );
      expect(
        () => StyleProfileState.fromJson({'profile_state': 'done'}),
        throwsFormatException,
      );
    });

    test('views follow profile_state when the server sends it', () {
      StyleProfileState of(ProfileState state, [ProfileAnalysis? analysis]) =>
          StyleProfileState(profileState: state, analysis: analysis);
      expect(profileViewOf(of(ProfileState.building)), ProfileView.analyzing);
      expect(profileViewOf(of(ProfileState.stale)), ProfileView.outdated);
      expect(
        profileViewOf(
          of(ProfileState.stale, analysisOf(AnalysisStatus.succeeded)),
        ),
        ProfileView.outdated,
      );
      expect(
        profileViewOf(
          of(ProfileState.stale, analysisOf(AnalysisStatus.running)),
        ),
        ProfileView.analyzing,
      );
      expect(
        profileViewOf(
          of(
            ProfileState.stale,
            analysisOf(AnalysisStatus.failed, 'op', 'source_changed'),
          ),
        ),
        ProfileView.sourceChanged,
      );
      expect(profileViewOf(of(ProfileState.none)), ProfileView.noAnalysis);
      // The records behind the last profile are gone: not "outdated".
      expect(
        profileViewOf(
          of(ProfileState.none, analysisOf(AnalysisStatus.succeeded)),
        ),
        ProfileView.noAnalysis,
      );
      expect(
        profileViewOf(
          of(
            ProfileState.none,
            analysisOf(AnalysisStatus.failed, 'op', 'insufficient_input'),
          ),
        ),
        ProfileView.needsRecords,
      );
    });

    test('profile_state decides when a new analysis may be requested', () {
      bool can(ProfileState state, [AnalysisStatus? status]) =>
          canRequestAnalysis(
            StyleProfileState(
              profileState: state,
              analysis: status == null ? null : analysisOf(status),
            ),
          );
      expect(can(ProfileState.stale), isTrue);
      expect(can(ProfileState.stale, AnalysisStatus.succeeded), isTrue);
      expect(can(ProfileState.stale, AnalysisStatus.running), isFalse);
      expect(can(ProfileState.building), isFalse);
      expect(can(ProfileState.building, AnalysisStatus.failed), isFalse);
      expect(can(ProfileState.none), isFalse);
      expect(can(ProfileState.none, AnalysisStatus.succeeded), isFalse);
      expect(can(ProfileState.none, AnalysisStatus.failed), isTrue);
    });

    test('a new analysis may be requested only after it ended badly', () {
      bool can(StyleProfile? profile, AnalysisStatus? status) =>
          canRequestAnalysis(
            StyleProfileState(
              profile: profile,
              analysis: status == null ? null : analysisOf(status),
            ),
          );
      final profile = sampleProfile();
      expect(can(null, null), isFalse); // the server starts the first one
      expect(can(null, AnalysisStatus.running), isFalse);
      expect(can(profile, AnalysisStatus.running), isFalse);
      expect(can(profile, AnalysisStatus.succeeded), isFalse);
      expect(can(null, AnalysisStatus.succeeded), isTrue); // outdated
      expect(can(null, AnalysisStatus.failed), isTrue);
      expect(can(profile, AnalysisStatus.failed), isTrue);
      expect(can(profile, AnalysisStatus.unknown), isTrue);
    });
  });

  group('HTTP', () {
    test('the lookup is one GET of the partner style profile', () async {
      final (service, requests) = await httpService(
        (request) async =>
            jsonResponse({'profile': profileJson(), 'analysis': null}),
      );
      final state = await service.styleProfile('p1');
      expect(state.profile!.version, 3);
      expect(requests.single.method, 'GET');
      expect(requests.single.url.path, '/personas/p1/style-profile');
      expect(
        requests.single.headers['Authorization'],
        'Bearer synthetic-token',
      );
    });

    test('an analysis request sends only the operation id', () async {
      final (service, requests) = await httpService(
        (request) async => jsonResponse(analysisJson('running'), 202),
      );
      final analysis = await service.requestAnalysis('p1', operationId: 'op-1');
      expect(analysis.running, isTrue);
      expect(requests.single.method, 'POST');
      expect(requests.single.url.path, '/personas/p1/style-profile/analysis');
      expect(jsonDecode(requests.single.body), {'operation_id': 'op-1'});
    });

    test('a refusal without confirmed records is passed on', () async {
      final (service, _) = await httpService(
        (request) async => jsonResponse({
          'error': {'code': 'INVALID_INPUT', 'retryable': false},
        }, 422),
      );
      await expectLater(
        service.requestAnalysis('p1', operationId: 'op-1'),
        throwsA(
          isA<BackendFailure>().having((f) => f.code, 'code', 'INVALID_INPUT'),
        ),
      );
    });
  });

  group('StyleProfileController', () {
    StyleProfileController controllerFor(FakePersonaService service) {
      var ids = 0;
      final controller = StyleProfileController(
        service,
        'p1',
        newOperationId: () => 'op-${++ids}',
      );
      addTearDown(controller.dispose);
      return controller;
    }

    test('an unanswered request is retried with the same id', () async {
      final service = FakePersonaService(personas: const [partner])
        ..analysisError = const BackendFailure(BackendFailureKind.network);
      final controller = controllerFor(service);
      await controller.load();
      await expectLater(controller.requestAnalysis(), throwsA(anything));
      service.analysisError = null;
      await controller.requestAnalysis();
      expect(service.analysisRequests.map((r) => r.operation), [
        'op-1',
        'op-1',
      ]);
      expect(controller.state!.analysis!.running, isTrue);

      // The next request is a new one.
      service.analysisResult = analysisOf(AnalysisStatus.failed);
      await controller.requestAnalysis();
      expect(service.analysisRequests.last.operation, 'op-2');
    });

    test('a refused request does not reuse its id', () async {
      final service = FakePersonaService(personas: const [partner])
        ..analysisError = const BackendFailure(
          BackendFailureKind.generic,
          code: 'INVALID_INPUT',
        );
      final controller = controllerFor(service);
      await expectLater(controller.requestAnalysis(), throwsA(anything));
      service.analysisError = null;
      await controller.requestAnalysis();
      expect(service.analysisRequests.map((r) => r.operation), [
        'op-1',
        'op-2',
      ]);
    });

    test('a quiet recheck keeps the state on screen when it fails', () async {
      final service = FakePersonaService(personas: const [partner])
        ..profileState = StyleProfileState(profile: sampleProfile());
      final controller = controllerFor(service);
      await controller.load();
      service.profileError = StateError('offline');
      await controller.load(quiet: true);
      expect(controller.status, LoadStatus.ready);
      expect(controller.state!.profile, isNotNull);
      await controller.load();
      expect(controller.status, LoadStatus.failed);
    });
  });

  group('SC-07 screen', () {
    testWidgets('shows the profile items, its date and the display name', (
      tester,
    ) async {
      await openProfile(
        tester,
        state: StyleProfileState(
          profile: sampleProfile(),
          analysis: analysisOf(AnalysisStatus.succeeded),
        ),
      );
      expect(find.text('엄마의 말투'), findsOneWidget);
      expect(find.text('10월 6일 갱신'), findsOneWidget);
      for (final item in ['밥은 먹었니', '우리 딸', '~하렴']) {
        expect(find.text(item), findsOneWidget);
      }
      expect(find.text('자주 쓰는 표현'), findsOneWidget);
      expect(find.text('문장 끝맺음'), findsOneWidget);
      expect(reanalyze, findsNothing);
      expect(find.text('대화 상대 이름 수정'), findsOneWidget);
    });

    testWidgets('empty item groups say so', (tester) async {
      await openProfile(
        tester,
        state: StyleProfileState(
          profile: StyleProfile(version: 1, updatedAt: DateTime.utc(2026)),
        ),
      );
      expect(find.text('추출된 항목이 없어요'), findsNWidgets(3));
    });

    testWidgets('never analyzed: asks for records, no re-analyze', (
      tester,
    ) async {
      await openProfile(tester);
      expect(find.text('아직 분석한 기록이 없어요'), findsOneWidget);
      expect(reanalyze, findsNothing);
      await tester.tap(find.text('기록 올리기'));
      await tester.pump();
      expect(find.text('기록 올리기는 아직 연결되지 않았어요.'), findsOneWidget);
    });

    testWidgets('a failed lookup can be retried', (tester) async {
      final service = FakePersonaService(personas: const [partner])
        ..profileError = StateError('offline');
      final personas = PersonaController(service);
      addTearDown(personas.dispose);
      await personas.load();
      await tester.pumpWidget(
        MaterialApp(
          home: SpeechProfileScreen(personas: personas, personaId: 'p1'),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('말투 프로필을 불러오지 못했어요.'), findsOneWidget);
      service
        ..profileError = null
        ..profileState = StyleProfileState(profile: sampleProfile());
      await tester.tap(find.text('다시 시도'));
      await tester.pumpAndSettle();
      expect(find.text('밥은 먹었니'), findsOneWidget);
    });

    testWidgets('a server without the style profile says it is not ready', (
      tester,
    ) async {
      final service = FakePersonaService(personas: const [partner])
        ..profileError = const BackendFailure(
          BackendFailureKind.generic,
          code: apiNotProvided,
        );
      final personas = PersonaController(service);
      addTearDown(personas.dispose);
      await personas.load();
      await tester.pumpWidget(
        MaterialApp(
          home: SpeechProfileScreen(personas: personas, personaId: 'p1'),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('말투 프로필은 아직 준비 중이에요'), findsOneWidget);
      expect(find.text('말투 프로필을 불러오지 못했어요.'), findsNothing);
      expect(find.text('다시 시도'), findsNothing);
      expect(reanalyze, findsNothing);
      // Renaming stays reachable; the partner itself is fine.
      expect(find.text('대화 상대 이름 수정'), findsOneWidget);

      // Once the server serves it, pulling down shows the profile.
      service
        ..profileError = null
        ..profileState = StyleProfileState(profile: sampleProfile());
      await tester.fling(find.byType(ListView), const Offset(0, 400), 1000);
      await tester.pumpAndSettle();
      expect(find.text('말투 프로필은 아직 준비 중이에요'), findsNothing);
      expect(find.text('밥은 먹었니'), findsOneWidget);
    });

    for (final (code, title) in [
      ('insufficient_input', '말투를 파악하려면 기록이 더 필요해요'),
      ('source_changed', '분석하는 동안 기록이 바뀌었어요'),
      ('ai_failed', '말투를 분석하지 못했어요'),
    ]) {
      testWidgets('a failed analysis ($code) explains itself', (tester) async {
        await openProfile(
          tester,
          state: StyleProfileState(
            analysis: analysisOf(AnalysisStatus.failed, 'op', code),
          ),
        );
        expect(find.text(title), findsOneWidget);
        expect(find.textContaining(code), findsNothing);
        expect(reanalyze, findsOneWidget);
      });
    }

    testWidgets('re-analyzing starts a job and then rechecks it', (
      tester,
    ) async {
      final h = await openProfile(
        tester,
        state: StyleProfileState(
          analysis: analysisOf(AnalysisStatus.failed, 'op', 'ai_failed'),
        ),
      );
      final calls = h.service.profileCalls;
      await tester.tap(reanalyze);
      await tester.pump();
      expect(h.service.analysisRequests, hasLength(1));
      expect(find.text('말투를 분석하고 있어요'), findsOneWidget);
      expect(find.text('말투 분석을 다시 시작했어요.'), findsOneWidget);
      expect(reanalyze, findsNothing);

      // The interim recheck reads the state again while it runs.
      h.service.profileState = StyleProfileState(
        profile: sampleProfile(),
        analysis: analysisOf(AnalysisStatus.succeeded),
      );
      await tester.pump(analysisCheckInterval);
      await tester.pumpAndSettle();
      expect(h.service.profileCalls, calls + 1);
      expect(find.text('밥은 먹었니'), findsOneWidget);

      // Nothing runs any more, so no further checks are scheduled.
      await tester.pump(analysisCheckInterval * 3);
      expect(h.service.profileCalls, calls + 1);
    });

    testWidgets('rechecks stop at the limit and offer a manual check', (
      tester,
    ) async {
      final h = await openProfile(
        tester,
        state: StyleProfileState(analysis: analysisOf(AnalysisStatus.running)),
      );
      final checks =
          analysisCheckLimit.inSeconds ~/ analysisCheckInterval.inSeconds;
      for (var i = 0; i < checks; i++) {
        await tester.pump(analysisCheckInterval);
      }
      await tester.pumpAndSettle();
      expect(h.service.profileCalls, 1 + checks);
      expect(find.text('상태 다시 확인'), findsOneWidget);
      await tester.pump(analysisCheckInterval * 4);
      expect(h.service.profileCalls, 1 + checks);

      h.service.profileState = StyleProfileState(profile: sampleProfile());
      await tester.tap(find.text('상태 다시 확인'));
      await tester.pumpAndSettle();
      expect(find.text('밥은 먹었니'), findsOneWidget);
    });

    testWidgets('leaving the screen stops the rechecks', (tester) async {
      final h = await openProfile(
        tester,
        state: StyleProfileState(analysis: analysisOf(AnalysisStatus.running)),
      );
      await tester.pumpWidget(const MaterialApp(home: SizedBox()));
      await tester.pump(analysisCheckInterval * 2);
      expect(h.service.profileCalls, 1);
    });

    testWidgets('a running analysis over a profile keeps showing it', (
      tester,
    ) async {
      await openProfile(
        tester,
        state: StyleProfileState(
          profile: sampleProfile(),
          analysis: analysisOf(AnalysisStatus.running),
        ),
      );
      expect(find.text('밥은 먹었니'), findsOneWidget);
      expect(find.textContaining('대화는 지금 프로필로 계속돼요'), findsOneWidget);
      expect(reanalyze, findsNothing);
    });

    testWidgets('an unknown result over a profile can be checked again', (
      tester,
    ) async {
      final h = await openProfile(
        tester,
        state: StyleProfileState(
          profile: sampleProfile(),
          analysis: analysisOf(AnalysisStatus.unknown),
        ),
      );
      expect(find.textContaining('마지막 분석 결과를 확인하지 못했어요'), findsOneWidget);
      // The server finds the result was published after all.
      h.service
        ..analysisResult = analysisOf(AnalysisStatus.succeeded)
        ..profileState = StyleProfileState(
          profile: sampleProfile(),
          analysis: analysisOf(AnalysisStatus.succeeded),
        );
      await tester.tap(reanalyze);
      await tester.pumpAndSettle();
      expect(find.text('분석이 이미 끝나 있었어요.'), findsOneWidget);
      expect(reanalyze, findsNothing);
    });

    testWidgets('a stale profile asks for a new analysis', (tester) async {
      await openProfile(
        tester,
        state: const StyleProfileState(profileState: ProfileState.stale),
      );
      expect(find.text('프로필을 새로 만들어야 해요'), findsOneWidget);
      expect(reanalyze, findsOneWidget);
    });

    testWidgets('a profile being built is rechecked without a job yet', (
      tester,
    ) async {
      final h = await openProfile(
        tester,
        state: const StyleProfileState(profileState: ProfileState.building),
      );
      expect(find.text('말투를 분석하고 있어요'), findsOneWidget);
      expect(reanalyze, findsNothing);
      expect(h.service.profileCalls, 1);
      h.service.profileState = StyleProfileState(
        profile: sampleProfile(),
        profileState: ProfileState.ready,
      );
      await tester.pump(analysisCheckInterval);
      await tester.pump();
      expect(h.service.profileCalls, 2);
      expect(find.text('말투를 분석하고 있어요'), findsNothing);
      expect(find.text('자주 쓰는 표현'), findsOneWidget);
    });

    testWidgets('an outdated profile asks for a new analysis', (tester) async {
      await openProfile(
        tester,
        state: StyleProfileState(
          analysis: analysisOf(AnalysisStatus.succeeded),
        ),
      );
      expect(find.text('프로필을 새로 만들어야 해요'), findsOneWidget);
      expect(reanalyze, findsOneWidget);
    });

    testWidgets('a refused request without records explains itself', (
      tester,
    ) async {
      final h = await openProfile(
        tester,
        state: StyleProfileState(
          analysis: analysisOf(AnalysisStatus.failed, 'op', 'ai_failed'),
        ),
      );
      h.service.analysisError = const BackendFailure(
        BackendFailureKind.generic,
        code: 'INVALID_INPUT',
      );
      await tester.tap(reanalyze);
      await tester.pumpAndSettle();
      expect(find.textContaining('확정된 기록이 없어요'), findsOneWidget);
      expect(find.text('말투를 분석하지 못했어요'), findsOneWidget);
      expect(reanalyze, findsOneWidget);
    });

    testWidgets('a request in flight blocks a second one', (tester) async {
      final h = await openProfile(
        tester,
        state: StyleProfileState(
          analysis: analysisOf(AnalysisStatus.failed, 'op', 'ai_failed'),
        ),
      );
      final gate = h.service.analysisGate = Completer<void>();
      await tester.tap(reanalyze);
      await tester.pump();
      final busy = find.widgetWithText(OutlinedButton, '요청하는 중…');
      expect(busy, findsOneWidget);
      expect(tester.widget<OutlinedButton>(busy).onPressed, isNull);
      await tester.tap(busy, warnIfMissed: false);
      await tester.pump();
      expect(h.service.analysisRequests, hasLength(1));
      gate.complete();
      await tester.pump();
      await tester.pump();
      expect(find.text('말투를 분석하고 있어요'), findsOneWidget);
    });

    testWidgets('rename goes to SC-13 and the title follows the new name', (
      tester,
    ) async {
      final h = await openProfile(tester);
      await tester.tap(find.text('대화 상대 이름 수정'));
      await tester.pumpAndSettle();
      expect(find.byType(PersonaNameScreen), findsOneWidget);
      expect(find.text('대화 상대 이름 수정'), findsWidgets);
      await tester.enterText(find.byType(TextField), '어머니');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, '저장'));
      await tester.pumpAndSettle();
      expect(find.byType(SpeechProfileScreen), findsOneWidget);
      expect(find.text('어머니의 말투'), findsOneWidget);
      expect(find.text('대화 상대 이름을 저장했어요.'), findsOneWidget);
      expect(h.service.updates.single.update.toJson(), {'display_name': '어머니'});
    });

    testWidgets('a rename cancelled in SC-13 changes nothing', (tester) async {
      final h = await openProfile(tester);
      await tester.tap(find.text('대화 상대 이름 수정'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '어머니');
      await tester.tap(find.text('취소'));
      await tester.pumpAndSettle();
      expect(find.text('엄마의 말투'), findsOneWidget);
      expect(h.service.updates, isEmpty);
    });
  });

  group('provider address', () {
    Future<void> openSheet(WidgetTester tester) async {
      await tester.tap(
        find.descendant(
          of: find.ancestor(
            of: find.text('나를 부르는 호칭'),
            matching: find.byType(Row),
          ),
          matching: find.byType(TextButton),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(ProviderAddressSheet), findsOneWidget);
    }

    testWidgets('shows the saved address with a way to change it', (
      tester,
    ) async {
      await openProfile(
        tester,
        persona: const Persona(id: 'p1', name: '김영희', providerAddress: '우리 딸'),
      );
      expect(find.text('나를 부르는 호칭'), findsOneWidget);
      expect(find.text('우리 딸'), findsOneWidget);
      expect(find.text('아직 정하지 않았어요'), findsNothing);
      expect(find.text('수정'), findsOneWidget);
    });

    testWidgets('unset shows a prompt to set it', (tester) async {
      await openProfile(
        tester,
        persona: const Persona(id: 'p1', name: '김영희'),
      );
      expect(find.text('아직 정하지 않았어요'), findsOneWidget);
      expect(find.text('정하기'), findsOneWidget);
    });

    testWidgets('a picked suggestion is saved as the address', (tester) async {
      final h = await openProfile(
        tester,
        persona: const Persona(id: 'p1', name: '김영희'),
        state: StyleProfileState(profile: sampleProfile()),
      );
      await openSheet(tester);
      expect(find.text('기록에서 찾은 호칭'), findsOneWidget);
      final sheetSaveButton = tester.widget<FilledButton>(sheetSave);
      expect(sheetSaveButton.onPressed, isNull); // nothing changed yet
      await tester.tap(
        find.descendant(
          of: find.byType(ProviderAddressSheet),
          matching: find.widgetWithText(ActionChip, '민지야'),
        ),
      );
      await tester.pump();
      await tester.tap(sheetSave);
      await tester.pumpAndSettle();
      expect(find.byType(ProviderAddressSheet), findsNothing);
      expect(h.service.updates.single.update.toJson(), {
        'provider_address': '민지야',
      });
      expect(find.text('나를 부르는 호칭을 저장했어요.'), findsOneWidget);
      expect(
        find.descendant(
          of: find.ancestor(
            of: find.text('나를 부르는 호칭'),
            matching: find.byType(Row),
          ),
          matching: find.text('민지야'),
        ),
        findsOneWidget,
      );
    });

    testWidgets('an empty field clears the address', (tester) async {
      final h = await openProfile(
        tester,
        persona: const Persona(id: 'p1', name: '김영희', providerAddress: '딸'),
      );
      await openSheet(tester);
      await tester.enterText(
        find.descendant(
          of: find.byType(ProviderAddressSheet),
          matching: find.byType(TextField),
        ),
        '   ',
      );
      await tester.pump();
      await tester.tap(sheetSave);
      await tester.pumpAndSettle();
      expect(h.service.updates.single.update.toJson(), {
        'provider_address': null,
      });
      expect(find.text('아직 정하지 않았어요'), findsOneWidget);
    });

    testWidgets('a failure keeps the sheet and the typed address', (
      tester,
    ) async {
      final h = await openProfile(
        tester,
        persona: const Persona(id: 'p1', name: '김영희'),
      );
      h.service.updateError = StateError('offline');
      await openSheet(tester);
      final field = find.descendant(
        of: find.byType(ProviderAddressSheet),
        matching: find.byType(TextField),
      );
      await tester.enterText(field, '우리 딸');
      await tester.pump();
      await tester.tap(sheetSave);
      await tester.pumpAndSettle();
      expect(find.byType(ProviderAddressSheet), findsOneWidget);
      expect(find.textContaining('저장하지 못했어요'), findsOneWidget);
      expect(tester.widget<TextField>(field).controller!.text, '우리 딸');
      expect(h.personas.current!.providerAddress, isNull);

      h.service.updateError = null;
      await tester.tap(sheetSave);
      await tester.pumpAndSettle();
      expect(find.byType(ProviderAddressSheet), findsNothing);
      expect(h.personas.current!.providerAddress, '우리 딸');
    });

    testWidgets('a server without the address API says so in the sheet', (
      tester,
    ) async {
      final h = await openProfile(
        tester,
        persona: const Persona(id: 'p1', name: '김영희'),
      );
      h.service.updateError = const BackendFailure(
        BackendFailureKind.generic,
        code: apiNotProvided,
      );
      await openSheet(tester);
      await tester.enterText(
        find.descendant(
          of: find.byType(ProviderAddressSheet),
          matching: find.byType(TextField),
        ),
        '우리 딸',
      );
      await tester.pump();
      await tester.tap(sheetSave);
      await tester.pumpAndSettle();
      expect(find.byType(ProviderAddressSheet), findsOneWidget);
      expect(find.textContaining('호칭 저장은 아직 준비 중이에요'), findsOneWidget);
      expect(h.personas.current!.providerAddress, isNull);
    });
    testWidgets('cancel changes nothing', (tester) async {
      final h = await openProfile(tester);
      await openSheet(tester);
      await tester.tap(
        find.descendant(
          of: find.byType(ProviderAddressSheet),
          matching: find.text('취소'),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.byType(ProviderAddressSheet), findsNothing);
      expect(h.service.updates, isEmpty);
    });
  });

  testWidgets('fits a 320 wide screen with long names and many items', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(320, 568);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    await openProfile(
      tester,
      persona: Persona(
        id: 'p1',
        name: '김영희',
        displayName: '가' * 50,
        providerAddress: '나' * 50,
      ),
      state: StyleProfileState(
        profile: StyleProfile(
          version: 1,
          updatedAt: DateTime.utc(2026, 10, 6),
          expressions: [for (var i = 0; i < 30; i++) '자주 쓰는 표현 $i'],
          addressTerms: const ['우리 딸'],
          endings: const ['~했니?'],
        ),
        analysis: analysisOf(AnalysisStatus.failed, 'op', 'ai_failed'),
      ),
    );
    expect(tester.takeException(), isNull);
    expect(reanalyze, findsOneWidget);
    expect(find.text('대화 상대 이름 수정'), findsOneWidget);
  });
}
