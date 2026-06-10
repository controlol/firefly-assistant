# Phase 2 Embeddings Spike — Findings

**Goal:** de-risk local, torch-free embedding of short Dutch merchant/transaction
strings for nearest-neighbour categorisation + merchant entity-resolution. No cloud APIs.

**Environment:** Windows 10, Python 3.14.5 (uv-managed), CPU-only. The project already
ships `onnxruntime==1.26.0` and `numpy==2.4.6` (for RapidOCR).

> Note: the brief referenced `docs/ENRICHMENT.md` (Phase 2 section). That file does
> not exist in the repo yet; context was taken from `PLAN.md` ("Later / maybe" →
> AI categorization fallback) instead.

---

## TL;DR / Recommendation

- **Model:** `intfloat/multilingual-e5-small` — 384-dim, **MIT** licence, strong Dutch.
- **Runtime:** **`fastembed`** (Qdrant), running the model's ONNX graph on the
  **existing `onnxruntime`**. **No torch.**
- **Measured (warm cache, this machine):** load **~1.1 s**, **~4.1 ms / embed**
  steady-state single string, **~1.4 ms / embed** in a batch of 32. Embedding dim 384.
- **Added dependency weight:** **~24 MB** of pure-Python/wheel deps on top of what the
  project already has (no torch, no CUDA). vs. **sentence-transformers ≈ +400–500 MB**
  installed (torch alone is a 117 MB wheel).
- **Accuracy on the held-out Dutch set:** e5-small got **6/6** correct, including unseen
  merchants (Aldi→groceries, Tango→fuel, Eneco→utilities). The fastembed-native
  `paraphrase-multilingual-MiniLM-L12-v2` got only **3/6**.

**Go for Phase 2 with fastembed + e5-small. It reuses onnxruntime cleanly; no heavy stack needed.**

---

## Runtime comparison: fastembed vs sentence-transformers

| | `fastembed` (Qdrant) | `sentence-transformers` |
|---|---|---|
| Inference backend | **onnxruntime** (already in project) | **torch** (new, heavy) |
| Pulls torch? | **No** | **Yes** — `torch==2.12.0` |
| Net-new install size | **~24 MB** (measured, see below) | **~400–500 MB** installed; torch wheel alone = **117.3 MB** download (Win cp314 amd64) |
| Net-new packages | fastembed, tokenizers, huggingface-hub, hf-xet, mmh3, py-rust-stemmers, loguru, typer/rich/click + small deps | the above **plus** torch, transformers, scipy, scikit-learn, sympy, networkx, joblib, safetensors, regex, jinja2, … (16 wheels would download; 41 resolved) |
| onnxruntime/numpy versions | `onnxruntime==1.26.0`, `numpy==2.4.6` — **identical to project** (clean reuse) | n/a (uses torch) |
| Custom ONNX models | `TextEmbedding.add_custom_model(...)` | via Optimum/manual export |
| Install time (measured) | **2.6 s** | not installed (dry-run resolve only) |

**Recommendation: `fastembed`.** It reuses the OCR onnxruntime/numpy versions exactly,
adds ~24 MB, and stays torch-free. sentence-transformers would roughly **double the
install footprint** and introduce the entire torch/transformers/scipy stack purely to run
the same small model — not justified for short-string embedding on CPU.

### Exact packages fastembed added (net-new vs the project venv)

Measured by installing `fastembed==0.8.0` into an isolated 3.14 venv and diffing
against the project venv. Packages **already present** in the project (and therefore
free): `onnxruntime`, `numpy`, `pillow`, `protobuf`, `flatbuffers`, `httpx`, `certifi`,
`idna`, `tqdm`, `pyyaml`.

Net-new (on-disk, approx):

| Package | MB | Package | MB |
|---|---|---|---|
| hf-xet | 9.3 | requests | 0.22 |
| tokenizers | 7.6 | loguru | 0.22 |
| huggingface-hub | 2.6 | filelock | 0.13 |
| rich | 1.2 | mmh3 | 0.12 |
| fsspec | 0.71 | fastembed | 0.36 |
| py-rust-stemmers | 0.54 | typer | 0.26 |
| urllib3 | 0.41 | charset-normalizer | 0.23 |
| click | 0.40 | + small (shellingham, markdown-it-py, mdurl, win32-setctime, annotated-doc) | ~0.1 |

**Total net-new ≈ 24 MB.** (Several of these — `hf-xet`, `huggingface-hub`, `tokenizers` —
are only needed to *download* the model; they are not on the embedding hot path.)

---

## Model comparison (small multilingual, short Dutch strings)

| Model | Params/size on disk | Dim | Dutch | Licence | CPU latency¹ | How it loads (fastembed) |
|---|---|---|---|---|---|---|
| **`intfloat/multilingual-e5-small`** ✅ | ~118M; ONNX fp32 = **448 MB** on disk² | 384 | **Strong** (e5 is heavily multilingual; mE5 covers nl) | **MIT** | **~4.1 ms** single / **~1.4 ms** batch-32 (measured) | **custom ONNX**: repo ships `onnx/model.onnx`; register via `add_custom_model` (MEAN pool, L2 norm, `query:`/`passage:` prefixes) |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | quantized ONNX = **240 MB** on disk | 384 | Good (50+ langs incl. nl) | apache-2.0 | **~3.9 ms** single (measured) | **built-in** to fastembed (ships a Qdrant **Q/quantized** ONNX) |
| `snowflake/snowflake-arctic-embed-xs` / `-s` | 90–130 MB | 384 | **No** — English-only; not usable for nl | apache-2.0 | n/a | built-in |
| `BAAI/bge-small-en-v1.5` | 67 MB | 384 | **No** — English (bge-small has only `-en`/`-zh` in fastembed) | mit | n/a | built-in |
| `intfloat/multilingual-e5-large` | 2.24 GB | 1024 | Strong | mit | not measured (too big for "small") | built-in |

¹ Steady-state, warm graph, this machine (Win10 CPU), via fastembed/onnxruntime.
² The original e5-small repo ships an **un-quantized** `onnx/model.onnx` (448 MB). A
quantized variant exists in the repo (`onnx/model_qint8_avx512_vnni.onnx`) but see the
gotcha below — fastembed's custom-model fetch did not pull it, so it was not benchmarked.

### Why e5-small over the fastembed-native paraphrase-MiniLM

Both are 384-dim and similar latency, **but quality differed sharply** on the held-out
Dutch set (same reference set, cosine k-NN, k=3):

| Held-out query | e5-small (recommended) | paraphrase-MiniLM (built-in) |
|---|---|---|
| Albert Heijn 2264 | groceries ✅ | groceries ✅ |
| Aldi Eindhoven | **groceries ✅** | fuel ❌ |
| Tango Tankstation | **fuel ✅** | dining ❌ |
| NS Groep IC | transport ✅ | transport ✅ |
| Eetcafe De Brug | dining ✅ | dining ✅ |
| Eneco Energie B.V. | **utilities ✅** | groceries ❌ |
| **Score** | **6/6** | **3/6** |

The built-in paraphrase model is the **quantized** Qdrant export and also emits a
mean-vs-CLS-pooling change warning on fastembed ≥0.8; both likely contribute to the
weaker result. e5-small (fp32 + correct `query:`/`passage:` prefixes) was clearly better
and is the recommendation.

---

## Measured run output (recommended path)

`experiments/.venv-fastembed/Scripts/python.exe experiments/embed_spike.py`

```
Model: intfloat/multilingual-e5-small (via fastembed custom ONNX, onnxruntime, no torch)
Load time (warm cache): 1.13s
Embedded 14 reference strings in 29.9ms (2.13ms/embed)
Steady-state single-string latency: 4.20ms/embed (n=50)
Embedding dim: 384

Held-out classification (cosine k-NN, k=3):
  query                    predicted     top_sim   margin
  Albert Heijn 2264        groceries       0.900    0.069
  Aldi Eindhoven           groceries       0.846    0.014
  Tango Tankstation        fuel            0.871    0.058
  NS Groep IC              transport       0.885    0.027
  Eetcafe De Brug          dining          0.863    0.025
  Eneco Energie B.V.       utilities       0.862    0.009
```

- First-ever run (cold, includes HF download of the 448 MB graph): load was **~28–31 s**.
  Warm-cache load is **~1.1 s**.
- `mypy --strict` and `ruff check` both pass on `embed_spike.py`.

---

## Windows / CPU gotchas (record for Phase 2)

1. **HF cache symlinks need Developer Mode/admin.** On first download fastembed/HF tries
   to create symlinks in the cache and fails with `WinError 1314: A required privilege is
   not held by the client`. It **retries and falls back** to copies automatically, so the
   download still succeeds — but it logs scary ERRORs. Mitigate by setting
   `HF_HUB_DISABLE_SYMLINKS_WARNING=1` and/or enabling Windows Developer Mode. Not a
   blocker.

2. **Confidence calibration for e5.** e5 produces high baseline cosine similarities
   (~0.85–0.92 even for correct matches) and therefore **small margins** between
   categories (0.01–0.07 here). For a confidence gate, prefer thresholding on the absolute
   `top_sim` (e.g. require > ~0.83) rather than only the inter-category margin. The
   paraphrase model gives wider margins but is less accurate — accuracy wins.

3. **e5 prefixes are mandatory.** Embed reference/known strings as `passage: <text>` and
   queries as `query: <text>`. Skipping the prefixes measurably degrades e5 quality. The
   prototype handles this.

4. **Quantized e5-small not obtained via fastembed.** The repo ships
   `onnx/model_qint8_avx512_vnni.onnx`, but `add_custom_model` + `model_file=...` did not
   pull that file into the snapshot (`NO_SUCHFILE` at load). The fp32 graph (448 MB on
   disk) works and is fast enough (~4 ms), so this was not pursued. **If disk footprint of
   the model matters**, export/quantize e5-small to int8 ONNX out-of-band (e.g. Optimum)
   and point `add_custom_model` at the local file — a follow-up, not a blocker.

5. **Pin notes.** fastembed `0.8.0`, onnxruntime `1.26.0`, numpy `2.4.6`, tokenizers
   `0.23.1`, huggingface-hub `1.18.0`. onnxruntime/numpy match the project's existing pins
   exactly, so adding `fastembed` should not perturb the OCR stack.

---

## How to reproduce

```powershell
# isolated venv (do not touch the project venv)
uv venv experiments\.venv-fastembed --python 3.14
uv pip install --python experiments\.venv-fastembed\Scripts\python.exe fastembed
experiments\.venv-fastembed\Scripts\python.exe experiments\embed_spike.py
```

The model auto-downloads on first run (~448 MB) into the HF/fastembed cache, then loads
from cache (~1.1 s) thereafter.

---

## Suggested Phase 2 shape (informed by this spike)

- Add `fastembed` as an **optional extra** (`[project.optional-dependencies] enrich`),
  keeping the core install OCR-only. It reuses onnxruntime/numpy.
- Embed known counterparties/categories once (`passage:`), persist the 384-dim vectors
  (numpy `.npy` or a tiny SQLite/Qdrant-lite store), and do cosine k-NN at enrich time
  (`query:`). For the data volumes here, a plain numpy matmul is more than fast enough.
- Use embedding similarity as the **fallback** after Firefly's deterministic rules, with a
  `top_sim` confidence gate and human-review flag below threshold (consistent with the
  existing "flag for manual review" pattern).
- Merchant entity-resolution: same embeddings, threshold cosine sim to cluster variants
  ("Albert Heijn 2264" ≈ "Albert Heijn 1234").
```
