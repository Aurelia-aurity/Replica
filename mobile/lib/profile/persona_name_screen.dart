import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../services/backend_service.dart';
import '../shared/conversation_widgets.dart';
import 'persona_controller.dart';
import 'persona_models.dart';
import 'persona_service.dart';

/// SC-13 프로필 만들기 · 대화 상대 이름 수정.
///
/// With [editing] null it creates a conversation partner (`POST /personas`).
/// Otherwise it changes the display name of [editing]
/// (`PATCH /personas/{id}` with `display_name`); the name given at creation,
/// the provider address and the records stay as they are. The screen pops
/// with the saved [Persona]; cancelling or going back pops with nothing and
/// changes nothing.
class PersonaNameScreen extends StatefulWidget {
  const PersonaNameScreen({super.key, required this.controller, this.editing});
  final PersonaController controller;
  final Persona? editing;

  @override
  State<PersonaNameScreen> createState() => _PersonaNameScreenState();
}

/// Height of the cancel/save row plus its padding and a small gap.
const _noticeLift = 88.0;

const _fieldLabel = '대화 상대 이름 또는 호칭';

final _cancelStyle = OutlinedButton.styleFrom(
  minimumSize: const Size.fromHeight(54),
  foregroundColor: const Color(0xFF17201C),
  backgroundColor: Colors.white,
  side: const BorderSide(color: Color(0xFFC9D0CC)),
  textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
);

final _saveStyle = FilledButton.styleFrom(
  minimumSize: const Size.fromHeight(54),
  backgroundColor: replicaGreen,
  disabledBackgroundColor: const Color(0xFFD5DBD8),
  disabledForegroundColor: const Color(0xFF8A948F),
  textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
);

class _PersonaNameScreenState extends State<PersonaNameScreen> {
  late final _text = TextEditingController(text: widget.editing?.label ?? '');
  bool _saving = false;
  // One id for this new partner until a request with it is definitely
  // refused. An attempt whose answer was lost may have stored the partner,
  // so even a changed name reuses its id: the server then refuses a second
  // partner with it, and the stored one is found instead.
  String _requestId = newPersonaId();

  /// The failure notice this screen is showing. Its "다시 시도" saves on this
  /// screen, so it must not outlive it: the notice lives in the app-wide
  /// [ScaffoldMessenger] and would otherwise stay on the screen below.
  ScaffoldFeatureController<SnackBar, SnackBarClosedReason>? _failureNotice;

  bool get _isEdit => widget.editing != null;
  String? get _name => normalizePersonaName(_text.text);
  bool get _canSave => !_saving && _name != null;

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  /// Takes the failure notice down, if one is up. Only this screen's own
  /// notice: any other one (such as the previous screen's) stays as it is.
  void _hideFailureNotice() {
    if (_failureNotice == null) return;
    _failureNotice = null;
    ScaffoldMessenger.of(context).hideCurrentSnackBar();
  }

  /// However the screen is left (cancel, the app bar's back button, the
  /// system back gesture, or a save), the failure notice goes with it.
  void _popped(bool didPop, Object? result) {
    if (didPop) _hideFailureNotice();
  }

  void _cancel() => Navigator.of(context).pop();

  String _failureText(Object error) => switch (error) {
    BackendFailure(code: apiNotProvided) =>
      '이름 수정은 아직 준비 중이에요. 지금은 처음 정한 이름으로 대화할 수 있어요.',
    BackendFailure(code: 'INVALID_INPUT') => '입력한 내용을 확인해 주세요.',
    _ => '저장하지 못했어요. 입력한 이름은 그대로 두었어요. 다시 시도해 주세요.',
  };

  /// Creates the partner under [_requestId]. When an earlier attempt with
  /// that id turns out to have stored it under another name, the typed
  /// [name] is applied as its display name; if that fails the partner is
  /// kept as stored rather than reported as a failure.
  Future<Persona> _create(String name) async {
    final saved = await widget.controller.create(name, personaId: _requestId);
    if (saved.label == name) return saved;
    try {
      return await widget.controller.update(
        saved.id,
        PersonaUpdate(displayName: name),
      );
    } catch (_) {
      return saved;
    }
  }

  Future<void> _save() async {
    // A notice action can fire after the screen is gone.
    if (!mounted) return;
    final name = _name;
    if (name == null || !_canSave) return;
    final editing = widget.editing;
    // Same name: nothing to send, just go back.
    if (editing != null && name == editing.label) {
      Navigator.of(context).pop(editing);
      return;
    }
    setState(() => _saving = true);
    try {
      final Persona saved;
      if (editing != null) {
        // Repeating the same update is harmless, so a retry needs no id.
        saved = await widget.controller.update(
          editing.id,
          PersonaUpdate(displayName: name),
        );
      } else {
        saved = await _create(name);
      }
      if (!mounted) return;
      // Clear the way for the notice below, as before; the pop that follows
      // then has no failure notice left to take down.
      _failureNotice = null;
      final messenger = ScaffoldMessenger.of(context)..hideCurrentSnackBar();
      if (saved.label != name) {
        // Stored by an earlier attempt under its name, and the new name could
        // not be applied: say so on the next screen.
        messenger.showSnackBar(
          SnackBar(
            content: Text(
              '앞서 저장한 "${saved.label}"(으)로 만들어졌어요. 이름은 말투 프로필에서 바꿀 수 있어요.',
            ),
          ),
        );
      }
      Navigator.of(context).pop(saved);
    } catch (error) {
      if (!mounted) return;
      if (!_isEdit && isDefiniteRejection(error)) {
        // Nothing was stored under this id: the next save may use a new one.
        _requestId = newPersonaId();
      }
      setState(() => _saving = false);
      final messenger = ScaffoldMessenger.of(context)..hideCurrentSnackBar();
      final notice = _failureNotice = messenger.showSnackBar(
        SnackBar(
          // Lifted above the button row so the failed name can be fixed and
          // saved again while the notice is still visible. The row sits above
          // the bottom safe area, so the notice has to clear it as well.
          behavior: SnackBarBehavior.floating,
          margin: EdgeInsets.fromLTRB(
            16,
            0,
            16,
            _noticeLift + MediaQuery.paddingOf(context).bottom,
          ),
          content: Text(_failureText(error)),
          // A retry cannot help while the server lacks the API.
          action: error is BackendFailure && error.code == apiNotProvided
              ? null
              : SnackBarAction(label: '다시 시도', onPressed: _save),
        ),
      );
      // Timed out or dismissed: nothing left to take down.
      notice.closed.then((_) {
        if (identical(_failureNotice, notice)) _failureNotice = null;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: !_saving,
      onPopInvokedWithResult: _popped,
      child: Scaffold(
        backgroundColor: replicaBackground,
        appBar: AppBar(
          backgroundColor: replicaBackground,
          surfaceTintColor: Colors.transparent,
          title: Text(
            _isEdit ? '대화 상대 이름 수정' : '프로필 만들기',
            style: const TextStyle(fontWeight: FontWeight.w700),
          ),
        ),
        body: SafeArea(
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 560),
              child: Column(
                children: [
                  Expanded(
                    child: SingleChildScrollView(
                      padding: const EdgeInsets.fromLTRB(24, 20, 24, 20),
                      keyboardDismissBehavior:
                          ScrollViewKeyboardDismissBehavior.onDrag,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            _isEdit ? '이름이나 호칭을\n바꿔 볼까요?' : '누구와\n대화하고 싶으세요?',
                            style: const TextStyle(
                              fontSize: 28,
                              fontWeight: FontWeight.w700,
                              height: 1.3,
                            ),
                          ),
                          const SizedBox(height: 10),
                          Text(
                            _isEdit
                                ? '앱에 보이는 이름과 대화에서 부를 이름이 바뀌어요. 올린 기록은 그대로예요.'
                                : '대화할 상대의 이름이나 호칭을 먼저 정해 주세요.',
                            style: const TextStyle(
                              color: Color(0xFF3D4743),
                              fontSize: 15,
                              height: 1.6,
                            ),
                          ),
                          const SizedBox(height: 32),
                          // The label sits above the field and the example stays
                          // visible under it, as in the screen plan.
                          const ExcludeSemantics(
                            child: Text(
                              _fieldLabel,
                              style: TextStyle(
                                fontSize: 13,
                                fontWeight: FontWeight.w600,
                                color: Color(0xFF3D4743),
                              ),
                            ),
                          ),
                          const SizedBox(height: 8),
                          Semantics(
                            label: _fieldLabel,
                            child: TextField(
                              controller: _text,
                              enabled: !_saving,
                              autofocus: true,
                              maxLength: personaNameMaxLength,
                              maxLengthEnforcement:
                                  MaxLengthEnforcement.enforced,
                              textInputAction: TextInputAction.done,
                              style: const TextStyle(
                                fontSize: 17,
                                fontWeight: FontWeight.w500,
                              ),
                              onChanged: (_) => setState(() {}),
                              onSubmitted: (_) => _save(),
                              decoration: InputDecoration(
                                helperText: '예: 엄마, 할머니, 민지',
                                helperStyle: const TextStyle(
                                  color: Color(0xFF5A6560),
                                  fontSize: 12,
                                ),
                                filled: true,
                                fillColor: Colors.white,
                                contentPadding: const EdgeInsets.symmetric(
                                  horizontal: 16,
                                  vertical: 17,
                                ),
                                border: OutlineInputBorder(
                                  borderRadius: BorderRadius.circular(12),
                                  borderSide: const BorderSide(
                                    color: replicaBorder,
                                  ),
                                ),
                              ),
                              buildCounter:
                                  (
                                    context, {
                                    required currentLength,
                                    required isFocused,
                                    required maxLength,
                                  }) => Text(
                                    '$currentLength / $maxLength',
                                    style: const TextStyle(
                                      color: Color(0xFF5A6560),
                                      fontSize: 12,
                                    ),
                                  ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(20, 12, 20, 16),
                    child: Row(
                      children: [
                        // 1 : 1.6 as in the screen plan.
                        Expanded(
                          flex: 10,
                          child: OutlinedButton(
                            style: _cancelStyle,
                            onPressed: _saving ? null : _cancel,
                            child: const Text('취소'),
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          flex: 16,
                          child: FilledButton(
                            style: _saveStyle,
                            onPressed: _canSave ? _save : null,
                            child: _saving
                                ? const SizedBox(
                                    width: 20,
                                    height: 20,
                                    child: CircularProgressIndicator(
                                      strokeWidth: 2,
                                      semanticsLabel: '저장 중',
                                    ),
                                  )
                                : const Text('저장'),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
