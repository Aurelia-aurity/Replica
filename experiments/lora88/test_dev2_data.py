"""DEV2 composition, ending-only rewrites and step metric regression tests."""

from collections import Counter
import math

from experiments.lora88.dev2_data import MIX, cases, diversity
from experiments.lora88.synthetic import REGISTERS, answer, content_signature
from experiments.lora88.test_train import setup
from experiments.lora88.train import TrainConfig, train_epochs


def test_dev2_composition_and_source_signatures() -> None:
    # Given / When
    authored = cases()
    # Then
    assert len(authored) == len({c.family for c in authored}) == 60
    assert Counter(c.kind for c in authored) == dict(MIX)
    assert sum(c.long for c in authored) * 3 == 72
    assert all(c.family.startswith("l88-dev2-train-") for c in authored)
    for case in authored:
        for register in REGISTERS:
            assert content_signature(case, answer(case, register), register) == case.stems
    for kind in ("multi", "ambiguity"):
        assert all(len(c.stems) == 2 and len(c.slots) == 4
                   for c in authored if c.kind == kind)
    for subgroup in range(3):
        selected = authored[36+subgroup*6:42+subgroup*6]
        assert len(selected) == 6
        assert len({c.stems for c in selected}) >= 4


def test_diversity_counts_actual_ngrams() -> None:
    # Given / When
    report = diversity(["a b c", "a b d", "a b c"])
    # Then
    assert report["rows"] == 3
    assert report["distinct_answers"] == 2
    assert report["word_ngrams"] == {"1": 4, "2": 3, "3": 2}


def test_step_metrics_report_finite_weighted_loss_and_gradient() -> None:
    # Given
    model, batches = setup()
    metrics = []
    # When
    result = train_epochs(
        model, batches, TrainConfig(epochs=1, accumulation=2, learning_rate=2e-5),
        on_metrics=lambda step, loss, grad: metrics.append((step, loss, grad)))
    # Then
    assert [r[0] for r in metrics] == [1, 2]
    assert all(math.isfinite(loss) and loss > 0 and math.isfinite(grad) and grad > 0
               for _, loss, grad in metrics)
    assert result[0].mean_loss == (metrics[0][1] * 4 + metrics[1][1] * 2) / 6
