"""Phase 2 embedding spike: local Dutch merchant-string categorisation via fastembed.

Runs an ONNX sentence-embedding model on the project's *existing* onnxruntime
dependency (NO torch). Embeds a handful of Dutch merchant strings, builds a tiny
labelled reference set, and classifies held-out strings with cosine k-NN,
printing the predicted category and a confidence margin.

Recommended model: intfloat/multilingual-e5-small (384-dim, MIT, strong Dutch).
It is not in fastembed's built-in catalogue, but the original HF repo ships a
plain ONNX graph (onnx/model.onnx) that we register via add_custom_model and run
straight on onnxruntime. e5 needs "query: " / "passage: " prefixes + MEAN
pooling + L2 normalisation (handled below).

Run (from repo root, using the spike venv):
    experiments/.venv-fastembed/Scripts/python.exe experiments/embed_spike.py

Typed; passes `mypy --strict` (fastembed ships inline types as of 0.8).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from fastembed import TextEmbedding
from fastembed.common.model_description import ModelSource, PoolingType

MODEL_HF_REPO = "intfloat/multilingual-e5-small"
MODEL_NAME = "custom/multilingual-e5-small"

FloatArray = npt.NDArray[np.float32]


# --- tiny labelled reference set: Dutch merchant string -> category ----------
REFERENCE: list[tuple[str, str]] = [
    ("Albert Heijn 1234", "groceries"),
    ("Jumbo Supermarkten Amsterdam", "groceries"),
    ("Lidl Nederland", "groceries"),
    ("Shell Station A2", "fuel"),
    ("BP Tankstation Utrecht", "fuel"),
    ("Esso Nederland", "fuel"),
    ("NS Reizigers", "transport"),
    ("GVB Amsterdam OV-Chipkaart", "transport"),
    ("Restaurant De Kas", "dining"),
    ("Cafe Belgie Utrecht", "dining"),
    ("Zettle_*RUIS", "dining"),  # iZettle card-reader payout, usually hospitality
    ("Example Vendor B.V.", "services"),
    ("KPN Mobiel", "utilities"),
    ("Vattenfall Energie", "utilities"),
]

# --- held-out strings to classify (deliberately noisy / abbreviated) ---------
HELD_OUT: list[str] = [
    "Albert Heijn 2264",   # -> groceries
    "Aldi Eindhoven",      # -> groceries (unseen merchant)
    "Tango Tankstation",   # -> fuel (unseen brand)
    "NS Groep IC",         # -> transport
    "Eetcafe De Brug",     # -> dining
    "Eneco Energie B.V.",  # -> utilities (unseen energy supplier)
]


@dataclass
class Prediction:
    category: str
    confidence: float  # cosine margin between best and 2nd-best category
    top_sim: float


def l2_normalise(mat: FloatArray) -> FloatArray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return (mat / norms).astype(np.float32)


def embed(model: TextEmbedding, texts: list[str], *, prefix: str) -> FloatArray:
    """Embed with the e5 instruction prefix and L2-normalise the result."""
    prefixed = [f"{prefix}{t}" for t in texts]
    vecs = np.asarray(list(model.embed(prefixed)), dtype=np.float32)
    return l2_normalise(vecs)


def classify(
    query_vec: FloatArray,
    ref_vecs: FloatArray,
    ref_labels: list[str],
    k: int = 3,
) -> Prediction:
    sims = ref_vecs @ query_vec  # cosine (all vectors L2-normalised)
    top_idx = np.argsort(-sims)[:k]

    per_cat: dict[str, float] = {}
    for i in top_idx:
        lbl = ref_labels[int(i)]
        per_cat[lbl] = max(per_cat.get(lbl, -1.0), float(sims[int(i)]))

    ranked = sorted(per_cat.items(), key=lambda kv: kv[1], reverse=True)
    best_cat, best_sim = ranked[0]
    margin = best_sim - (ranked[1][1] if len(ranked) > 1 else 0.0)
    return Prediction(category=best_cat, confidence=margin, top_sim=best_sim)


def build_model() -> TextEmbedding:
    TextEmbedding.add_custom_model(
        model=MODEL_NAME,
        sources=ModelSource(hf=MODEL_HF_REPO),
        model_file="onnx/model.onnx",
        pooling=PoolingType.MEAN,
        normalization=True,
        dim=384,
    )
    return TextEmbedding(model_name=MODEL_NAME)


def main() -> None:
    print(f"Model: {MODEL_HF_REPO} (via fastembed custom ONNX, onnxruntime, no torch)")

    t0 = time.perf_counter()
    model = build_model()
    print(f"Load time (warm cache): {time.perf_counter() - t0:.2f}s")

    ref_texts = [t for t, _ in REFERENCE]
    ref_labels = [c for _, c in REFERENCE]

    _ = embed(model, ["warmup"], prefix="query: ")  # pay graph-warmup cost once

    t0 = time.perf_counter()
    ref_vecs = embed(model, ref_texts, prefix="passage: ")
    ref_s = time.perf_counter() - t0
    print(
        f"Embedded {len(ref_texts)} reference strings in {ref_s * 1000:.1f}ms "
        f"({ref_s / len(ref_texts) * 1000:.2f}ms/embed)"
    )

    reps = 50
    t0 = time.perf_counter()
    for _ in range(reps):
        _ = embed(model, ["Albert Heijn 2264"], prefix="query: ")
    single_s = (time.perf_counter() - t0) / reps
    print(f"Steady-state single-string latency: {single_s * 1000:.2f}ms/embed (n={reps})")
    print(f"Embedding dim: {ref_vecs.shape[1]}")
    print()

    held_vecs = embed(model, HELD_OUT, prefix="query: ")
    print("Held-out classification (cosine k-NN, k=3):")
    print(f"  {'query':<24} {'predicted':<12} {'top_sim':>8} {'margin':>8}")
    for q, qv in zip(HELD_OUT, held_vecs, strict=True):
        pred = classify(qv, ref_vecs, ref_labels, k=3)
        print(f"  {q:<24} {pred.category:<12} {pred.top_sim:>8.3f} {pred.confidence:>8.3f}")


if __name__ == "__main__":
    main()
