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
  // One id per attempted name: a retry of the same save must not create a
  // second partner, a different name is a different request.
  String _requestId = newPersonaId();
  String? _requestName;

  bool get _isEdit => widget.editing != null;
  String? get _name => normalizePersonaName(_text.text);
  bool get _canSave => !_saving && _name != null;

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  void _cancel() {
    ScaffoldMessenger.of(context).hideCurrentSnackBar();
    Navigator.of(context).pop();
  }

  String _failureText(Object error) {
    if (error is BackendFailure && error.code == 'INVALID_INPUT') {
      return '입력한 내용을 확인해 주세요.';
    }
    return '저장하지 못했어요. 입력한 이름은 그대로 두었어요. 다시 시도해 주세요.';
  }

  Future<void> _save() async {
    // A notice action can fire after the screen is gone.
    if (!mounted) return;
    final name = _name;
    if (name == null || !_canSave) return;
    final editing = widget.editing;
    // Same name: nothing to send, just go back.
    if (editing != null && name == editing.label) {
      ScaffoldMessenger.of(context).hideCurrentSnackBar();
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
        if (_requestName != name) {
          _requestId = newPersonaId();
          _requestName = name;
        }
        saved = await widget.controller.create(name, personaId: _requestId);
      }
      if (!mounted) return;
      ScaffoldMessenger.of(context).hideCurrentSnackBar();
      Navigator.of(context).pop(saved);
    } catch (error) {
      if (!mounted) return;
      setState(() => _saving = false);
      ScaffoldMessenger.of(context)
        ..hideCurrentSnackBar()
        ..showSnackBar(
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
            action: SnackBarAction(label: '다시 시도', onPressed: _save),
          ),
        );
    }
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: !_saving,
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
