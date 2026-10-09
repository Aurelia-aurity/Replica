import 'package:flutter/material.dart';

const replicaGreen = Color(0xFF25634F);
const replicaBackground = Color(0xFFF2F5F4);
const replicaBorder = Color(0xFFE0E6E3);
const aiAmber = Color(0xFF9C5818);
const aiAmberBackground = Color(0xFFFFF0D9);

class AiBadge extends StatelessWidget {
  const AiBadge({super.key, this.label = 'AI 생성'});
  final String label;

  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
    decoration: BoxDecoration(
      color: aiAmberBackground,
      borderRadius: BorderRadius.circular(20),
    ),
    child: Text(label, style: const TextStyle(
      color: aiAmber, fontSize: 12, fontWeight: FontWeight.w600,
    )),
  );
}
