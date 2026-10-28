import 'package:flutter/material.dart';

import 'chat_reply.dart';
import '../shared/conversation_widgets.dart';

Future<void> showReplyEvidence(BuildContext context, ChatReply reply) =>
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.white,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
      builder: (context) => SafeArea(
        child: SizedBox(
          height: MediaQuery.sizeOf(context).height * .75,
          child: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 720),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(20, 16, 8, 8),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            '근거 기록 ${reply.sources.length}',
                            style: const TextStyle(
                              fontSize: 20,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                        IconButton(
                          tooltip: '근거 기록 닫기',
                          onPressed: () => Navigator.pop(context),
                          icon: const Icon(Icons.close),
                        ),
                      ],
                    ),
                  ),
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 20),
                    child: Text(
                      '답변에 사용된 기록 조각이에요.',
                      style: TextStyle(color: Color(0xFF68766F)),
                    ),
                  ),
                  const SizedBox(height: 12),
                  Expanded(
                    child: ListView.separated(
                      padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
                      itemCount: reply.sources.length,
                      separatorBuilder: (_, _) => const SizedBox(height: 12),
                      itemBuilder: (context, index) {
                        final source = reply.sources[index];
                        final date = source.date.toLocal();
                        return Container(
                          padding: const EdgeInsets.all(16),
                          decoration: BoxDecoration(
                            color: replicaBackground,
                            borderRadius: BorderRadius.circular(16),
                            border: Border.all(color: replicaBorder),
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                source.fileName,
                                style: const TextStyle(
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                              const SizedBox(height: 4),
                              Text(
                                '${date.year}년 ${date.month}월 ${date.day}일',
                                style: const TextStyle(
                                  fontSize: 12,
                                  color: Color(0xFF68766F),
                                ),
                              ),
                              const SizedBox(height: 12),
                              SelectableText(
                                source.excerpt,
                                style: const TextStyle(height: 1.6),
                              ),
                            ],
                          ),
                        );
                      },
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );

class NoEvidenceBorder extends CustomPainter {
  const NoEvidenceBorder();
  @override
  void paint(Canvas canvas, Size size) {
    final path = Path()
      ..addRRect(
        RRect.fromRectAndRadius(
          Rect.fromLTWH(.5, .5, size.width - 1, size.height - 1),
          const Radius.circular(20),
        ),
      );
    final paint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1
      ..color = const Color(0xFFA7B2AC);
    for (final metric in path.computeMetrics()) {
      for (double distance = 0; distance < metric.length; distance += 9) {
        canvas.drawPath(
          metric.extractPath(distance, (distance + 5).clamp(0, metric.length)),
          paint,
        );
      }
    }
  }

  @override
  bool shouldRepaint(NoEvidenceBorder oldDelegate) => false;
}
