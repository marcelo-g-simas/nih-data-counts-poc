"""Stage 7: residual PII-leakage QA gate. [Req 3 halt-on-anomaly, 9, 13]

Independently scans the de-identified + tokenized output with three
detectors that are deliberately *not* the de-id engine (stage 5) or the PPRL
identity attributes (stage 6): Presidio (spaCy-backed NER + regex
recognizers), GLiNER (a zero-shot NER checkpoint served via ONNX Runtime,
CPU-only), and Philter (fully offline regex/POS-based clinical text
de-identifier). Tuned for recall over precision: ANY hit from ANY detector
halts transmission of that batch and routes it to data/review_queue/.

GLiNER / runtime note: this used to load GLiNER through the `gliner` package's
own ONNX support, which -- verified directly against its installed source --
still imports PyTorch unconditionally at the package level (model.py,
onnx/model.py, decoding/decoder.py all `import torch` for tokenization/
collation/decoding glue), so it never actually shrank the dependency
footprint; PyTorch, transformers, and onnx were all still real runtime
dependencies. We instead now load the identical `onnx-community/
gliner_small-v2.1` checkpoint (a community fp32 ONNX export of the same
`urchade/gliner_small-v2.1` weights this pipeline always used) through
`fast_gliner` (github.com/talmago/fast_gliner), a Rust (`gline-rs`) + ONNX
Runtime binding whose only dependency is `huggingface_hub` -- no PyTorch,
transformers, or onnx package anywhere, at setup time or run time. Verified,
not assumed: a side-by-side comparison against the prior `gliner`+local-ONNX
path over this module's own fixtures produced byte-identical entity spans
and scores agreeing to 6 decimal places (same weights, different runtime), so
this is a dependency-footprint and speed change, not a detector-behavior
change -- ~1.5-1.6x faster per call in our own measurement (the README's
"~4x vs PyTorch" claim isn't the applicable comparison, since we were already
on ONNX Runtime, not eager PyTorch, before this switch).

Encoder NER only -- no vLLM or any generative-LLM serving stack anywhere.
"""
from __future__ import annotations

import json
from pathlib import Path

GLINER_MODEL_ID = "onnx-community/gliner_small-v2.1"
GLINER_LABELS = ["person", "phone number", "email", "address", "date of birth", "social security number", "medical record number"]
GLINER_ONNX_DIR = Path("models/gliner_onnx")
# Path *within* GLINER_ONNX_DIR once populated -- mirrors the onnx-community repo's own
# layout (an `onnx/` subdirectory), which is also fast_gliner's default `onnx_path`.
GLINER_ONNX_SUBPATH = "onnx/model.onnx"
# Presidio's own default nlp engine config wants en_core_web_lg (large, slow to fetch);
# pin it to the small model instead. It's a real pinned dependency in pyproject.toml
# (see the `[tool.uv.sources]` comment there for why), so plain `uv sync` installs it.
PRESIDIO_SPACY_MODEL = "en_core_web_sm"

_gliner_model = None
_presidio_analyzer = None
_philter_patterns = None


def _get_presidio():
    global _presidio_analyzer
    if _presidio_analyzer is None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        nlp_engine = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": PRESIDIO_SPACY_MODEL}],
        }).create_engine()
        _presidio_analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
    return _presidio_analyzer


def export_gliner_onnx(save_dir: str | Path = GLINER_ONNX_DIR) -> dict:
    """One-time, online download step (part of `setup-models`): pull the
    community fp32 ONNX export (tokenizer/config files + `onnx/model.onnx`
    only -- not the int8/fp16/bnb4/q4 variants also in that repo, to keep the
    offline model directory small and unambiguous) so `_get_gliner()` never
    needs network access again. No PyTorch/export step involved -- the ONNX
    file is already built upstream; this only downloads it.
    """
    from huggingface_hub import snapshot_download
    save_dir = Path(save_dir)
    local_dir = snapshot_download(
        repo_id=GLINER_MODEL_ID,
        local_dir=str(save_dir),
        allow_patterns=["*.json", "*.model", "*.txt", GLINER_ONNX_SUBPATH],
    )
    return {"onnx_path": str(Path(local_dir) / GLINER_ONNX_SUBPATH)}


def _get_gliner():
    global _gliner_model
    if _gliner_model is None:
        from fast_gliner import FastGLiNER
        onnx_path = GLINER_ONNX_DIR / GLINER_ONNX_SUBPATH
        if onnx_path.exists():
            # Rust (gline-rs) + ONNX Runtime backend, CPU-only.
            _gliner_model = FastGLiNER.from_pretrained(
                str(GLINER_ONNX_DIR), onnx_path=GLINER_ONNX_SUBPATH, execution_provider="cpu"
            )
        else:
            # Fallback for dev/test use before `setup-models` has run -- resolves and
            # downloads the model from the Hub on first use instead of failing.
            _gliner_model = FastGLiNER.from_pretrained(model_id=GLINER_MODEL_ID, execution_provider="cpu")
    return _gliner_model


def _get_philter_patterns():
    global _philter_patterns
    if _philter_patterns is None:
        import philter_lite
        config_path = Path(philter_lite.__file__).parent / "configs" / "philter_delta.toml"
        _philter_patterns = philter_lite.load_filters(str(config_path))
    return _philter_patterns


def scan_text(text: str) -> list[dict]:
    """Run all three independent detectors over `text`; return every hit with offsets.

    Presidio/GLiNER/Philter all report spans without modifying the input text.
    """
    hits: list[dict] = []

    for r in _get_presidio().analyze(text=text, language="en"):
        hits.append({"detector": "presidio", "entity_type": r.entity_type, "start": r.start, "end": r.end, "score": r.score})

    for ent in _get_gliner().predict_entities(text, GLINER_LABELS):
        hits.append({"detector": "gliner", "entity_type": ent["label"], "start": ent["start"], "end": ent["end"], "score": ent.get("score")})

    import philter_lite
    _, _, tracker = philter_lite.detect_phi(text, _get_philter_patterns())
    for phi in tracker.phi:
        hits.append({"detector": "philter", "entity_type": phi.phi_type, "start": phi.start, "end": phi.stop, "score": None})

    return hits


def scan_batch(records: list[dict], text_field: str = "note_text") -> dict:
    """Scan a batch of records (each optionally carrying a free-text field).

    Returns {"halt": bool, "flagged": [...]} -- any hit halts the whole batch.
    """
    flagged = []
    for record in records:
        text = record.get(text_field)
        if not text:
            continue
        hits = scan_text(text)
        if hits:
            flagged.append({"record_id": record.get("id"), "text": text, "hits": hits})
    return {"halt": len(flagged) > 0, "flagged": flagged}


def run(outbox_candidate: list[dict], text_field: str = "note_text",
        review_queue_dir: str | Path = "data/review_queue", batch_id: str = "batch") -> dict:
    result = scan_batch(outbox_candidate, text_field=text_field)
    if result["halt"]:
        review_queue_dir = Path(review_queue_dir)
        review_queue_dir.mkdir(parents=True, exist_ok=True)
        review_path = review_queue_dir / f"{batch_id}.json"
        review_path.write_text(json.dumps(result["flagged"], indent=2, default=str))
        result["review_path"] = str(review_path)
    return result
