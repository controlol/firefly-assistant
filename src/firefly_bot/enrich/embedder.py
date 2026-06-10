"""Embedding seam: turn short merchant/transaction strings into L2-normalised vectors.

`Embedder` is an injected Protocol (mirroring `TextRecogniser` / `Ledger`): the real
`E5Embedder` runs `intfloat/multilingual-e5-small` via fastembed on the project's existing
onnxruntime (NO torch — see experiments/EMBEDDINGS_SPIKE.md), and `FakeEmbedder` is a
deterministic, network-free stand-in for unit tests.

e5 is asymmetric: stored/"known" text is embedded with a ``passage:`` prefix and look-up text
with a ``query:`` prefix. The Protocol exposes that split explicitly so callers can't forget it.
All vectors are L2-normalised, so cosine similarity is a plain dot product.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

# e5 instruction prefixes (mandatory — skipping them measurably degrades quality).
_PASSAGE_PREFIX = "passage: "
_QUERY_PREFIX = "query: "

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def l2_normalise(mat: NDArray[np.float32]) -> NDArray[np.float32]:
    """L2-normalise each row, so cosine similarity reduces to a dot product."""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return (mat / norms).astype(np.float32)


class Embedder(Protocol):
    """Anything that maps short strings to L2-normalised float32 vectors.

    Two methods because e5 is asymmetric: known/stored examples are *passages*, look-up
    strings are *queries*. Both return a matrix shaped ``(len(texts), dim)``.
    """

    def embed_passages(self, texts: list[str]) -> NDArray[np.float32]: ...

    def embed_queries(self, texts: list[str]) -> NDArray[np.float32]: ...


class E5Embedder:
    """Default embedder: `multilingual-e5-small` via fastembed (custom ONNX, onnxruntime, no torch).

    The model is registered + loaded lazily on first use so constructing the embedder is cheap
    and import-time has no side effects. e5-small isn't in fastembed's catalogue, but its HF repo
    ships a plain ``onnx/model.onnx`` we register with MEAN pooling + L2 norm (per the spike).
    """

    _MODEL_HF_REPO = "intfloat/multilingual-e5-small"
    _MODEL_NAME = "custom/multilingual-e5-small"
    _DIM = 384

    def __init__(self, *, model_name: str = _MODEL_HF_REPO) -> None:
        self._hf_repo = model_name
        self._model: object | None = None

    def _ensure_model(self) -> object:
        if self._model is None:
            from fastembed import TextEmbedding
            from fastembed.common.model_description import ModelSource, PoolingType

            TextEmbedding.add_custom_model(
                model=self._MODEL_NAME,
                sources=ModelSource(hf=self._hf_repo),
                model_file="onnx/model.onnx",
                pooling=PoolingType.MEAN,
                normalization=True,
                dim=self._DIM,
            )
            self._model = TextEmbedding(model_name=self._MODEL_NAME)
        return self._model

    def _embed(self, texts: list[str], *, prefix: str) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self._DIM), dtype=np.float32)
        model = self._ensure_model()
        prefixed = [f"{prefix}{t}" for t in texts]
        vecs = np.asarray(list(model.embed(prefixed)), dtype=np.float32)  # type: ignore[attr-defined]
        return l2_normalise(vecs)

    def embed_passages(self, texts: list[str]) -> NDArray[np.float32]:
        return self._embed(texts, prefix=_PASSAGE_PREFIX)

    def embed_queries(self, texts: list[str]) -> NDArray[np.float32]:
        return self._embed(texts, prefix=_QUERY_PREFIX)


class FakeEmbedder:
    """Deterministic, network-free embedder for unit tests.

    Each string is reduced to its set of lowercased alphanumeric tokens, and each token is hashed
    to one component of a fixed-width vector (summed, then L2-normalised). So strings that share
    tokens land close in cosine space and strings that share *all* tokens collide exactly — which
    is enough to exercise the k-NN / zero-shot cascade without a model or network.

    Passages and queries are embedded identically (the fake has no asymmetry), so a query collides
    with a passage of the same token set — exactly what the deterministic tests rely on.
    """

    def __init__(self, *, dim: int = 1024) -> None:
        self._dim = dim

    def _vectorise(self, text: str) -> NDArray[np.float32]:
        vec = np.zeros(self._dim, dtype=np.float32)
        for token in sorted(set(_TOKEN_RE.findall(text.lower()))):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self._dim
            vec[idx] += 1.0
        return vec

    def _embed(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        mat = np.vstack([self._vectorise(t) for t in texts]).astype(np.float32)
        return l2_normalise(mat)

    def embed_passages(self, texts: list[str]) -> NDArray[np.float32]:
        return self._embed(texts)

    def embed_queries(self, texts: list[str]) -> NDArray[np.float32]:
        return self._embed(texts)
