import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../services/backend_service.dart';
import '../shared/conversation_widgets.dart';
import 'persona_controller.dart';
import 'persona_models.dart';
import 'persona_service.dart';

/// SC-13 말투 프로필 만들기 · 대화 상대 이름 수정.
///
/// With [editing] null it creates a conversation partner; otherwise it renames
/// [editing] and the profile items stay as they are. The screen pops with the
/// saved [Persona]; cancelling or going back pops with nothing and changes
/// nothing.
class PersonaNameScreen extends StatefulWidget {
  const PersonaNameScreen({super.key, required this.controller, this.editing});
  final PersonaController controller;
  final Persona? editing;

  @override
  State<PersonaNameScreen> createState() => _PersonaNameScreenState();
}

/// Height of the cancel/save row plus its padding and a small gap.
const _noticeLift = 88.0;

class _PersonaNameScreenState extends State<PersonaNameScreen> {
  late final _text = TextEditingController(text: widget.editing?.name ?? '');
  bool _saving = false;
  // One id per attempted name: a retry of the same save must not create a
  // second partner, a different name is a different request.
  String _requestId = newPersonaId();
  String? _requestName;

  bool get _isEdit => widget.editing != null;
  bool get _renameSupported => widget.controller.service.supportedFeatures
      .contains(PersonaFeature.rename);
  String? get _name => normalizePersonaName(_text.text);
  bool get _canSave =>
      !_saving && _name != null && (!_isEdit || _renameSupported);

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
    if (error is PersonaFeatureUnavailable) {
      return '이름 수정은 서버 연결이 준비되면 사용할 수 있어요.';
    }
    if (error is BackendFailure && error.code == 'INVALID_INPUT') {
      return '입력한 내용을 확인해 주세요.';
    }
    return '저장하지 못했어요. 입력한 이름은 그대로 두었어요. 다시 시도해 주세요.';
  }

  Future<void> _save() async {
    final name = _name;
    if (name == null || !_canSave) return;
    setState(() => _saving = true);
    try {
      final Persona saved;
      if (_isEdit) {
        // Same name: nothing to send, just go back to SC-07.
        saved = name == widget.editing!.name
            ? widget.editing!
            : await widget.controller.rename(name);
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
            // saved again while the notice is still visible.
            behavior: SnackBarBehavior.floating,
            margin: const EdgeInsets.fromLTRB(16, 0, 16, _noticeLift),
            content: Text(_failureText(error)),
            action: error is PersonaFeatureUnavailable
                ? null
                : SnackBarAction(label: '다시 시도', onPressed: _save),
          ),
        );
    }
  }

  @override
  Widget build(BuildContext context) {
    final title = _isEdit ? '대화 상대 이름 수정' : '말투 프로필 만들기';
    return PopScope(
      canPop: !_saving,
      child: Scaffold(
        backgroundColor: replicaBackground,
        appBar: AppBar(
          backgroundColor: replicaBackground,
          surfaceTintColor: Colors.transparent,
          title: Text(
            title,
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
                      padding: const EdgeInsets.fromLTRB(20, 12, 20, 20),
                      keyboardDismissBehavior:
                          ScrollViewKeyboardDismissBehavior.onDrag,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            _isEdit
                                ? '이름이나 호칭을\n바꿔 볼까요?'
                                : '누구의 말투로\n대화하고 싶으세요?',
                            style: const TextStyle(
                              fontSize: 24,
                              fontWeight: FontWeight.w700,
                              height: 1.3,
                            ),
                          ),
                          const SizedBox(height: 12),
                          Text(
                            _isEdit
                                ? '대화 상대의 이름이나 호칭을 바꿔요. 말투 프로필의 표현·호칭·끝맺음은 그대로 유지돼요.'
                                : '아직 말투 프로필이 없어요. 학습시킬 대화 상대의 이름이나 호칭을 먼저 정해 주세요.',
                            style: const TextStyle(
                              color: Color(0xFF68766F),
                              height: 1.5,
                            ),
                          ),
                          if (_isEdit && !_renameSupported) ...[
                            const SizedBox(height: 16),
                            const _Notice(
                              '이름 수정은 서버 연결이 준비되면 사용할 수 있어요. 지금은 저장할 수 없어요.',
                            ),
                          ],
                          const SizedBox(height: 24),
                          TextField(
                            controller: _text,
                            enabled: !_saving,
                            autofocus: true,
                            maxLength: personaNameMaxLength,
                            maxLengthEnforcement: MaxLengthEnforcement.enforced,
                            textInputAction: TextInputAction.done,
                            onChanged: (_) => setState(() {}),
                            onSubmitted: (_) => _save(),
                            decoration: InputDecoration(
                              labelText: '대화 상대 이름 또는 호칭',
                              hintText: '예: 엄마, 할머니, 민지',
                              filled: true,
                              fillColor: Colors.white,
                              border: OutlineInputBorder(
                                borderRadius: BorderRadius.circular(14),
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
                                    color: Color(0xFF68766F),
                                    fontSize: 12,
                                  ),
                                ),
                          ),
                        ],
                      ),
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(20, 8, 20, 16),
                    child: Row(
                      children: [
                        Expanded(
                          child: OutlinedButton(
                            onPressed: _saving ? null : _cancel,
                            child: const Text('취소'),
                          ),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: FilledButton(
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

class _Notice extends StatelessWidget {
  const _Notice(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(14),
    decoration: BoxDecoration(
      color: aiAmberBackground,
      borderRadius: BorderRadius.circular(14),
    ),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Icon(Icons.info_outline, size: 18, color: aiAmber),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            text,
            style: const TextStyle(color: aiAmber, fontSize: 13, height: 1.5),
          ),
        ),
      ],
    ),
  );
}
