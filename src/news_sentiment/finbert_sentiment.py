"""FinBERT-based financial sentiment (optional: requires transformers + torch)."""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_MODEL = None
_TOKENIZER = None
_DEVICE = -1


def _load_model(model_id: str) -> tuple[Any, Any]:
    global _MODEL, _TOKENIZER, _DEVICE
    if _MODEL is not None and _TOKENIZER is not None:
        return _MODEL, _TOKENIZER
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as e:
        raise RuntimeError(
            "FinBERT requires optional deps: pip install -r requirements-news.txt"
        ) from e

    _TOKENIZER = AutoTokenizer.from_pretrained(model_id)
    _MODEL = AutoModelForSequenceClassification.from_pretrained(model_id)
    _MODEL.eval()
    _DEVICE = 0 if torch.cuda.is_available() else -1
    if _DEVICE == 0:
        _MODEL = _MODEL.cuda()
    return _MODEL, _TOKENIZER


def score_texts(texts: list[str], model_id: str = "ProsusAI/finbert", max_length: int = 512) -> float:
    """
    Average sentiment in approximately [-1, 1]: P(positive) - P(negative)
    from FinBERT 3-way classification (positive / negative / neutral).
    """
    if not texts:
        return 0.0
    try:
        import torch
        import torch.nn.functional as F
    except ImportError:
        return 0.0

    model, tokenizer = _load_model(model_id)
    id2label = getattr(model.config, "id2label", None) or {0: "LABEL_0", 1: "LABEL_1", 2: "LABEL_2"}
    pos_idx = neg_idx = None
    for i, lab in id2label.items():
        low = str(lab).lower()
        if "pos" in low:
            pos_idx = int(i)
        if "neg" in low:
            neg_idx = int(i)
    if pos_idx is None or neg_idx is None:
        # ProsusAI/finbert: 0 positive, 1 negative, 2 neutral
        pos_idx, neg_idx = 0, 1
    n_labels = int(getattr(model.config, "num_labels", 3) or 3)
    pos_idx = max(0, min(pos_idx, n_labels - 1))
    neg_idx = max(0, min(neg_idx, n_labels - 1))

    scores: list[float] = []
    device = torch.device("cuda" if _DEVICE == 0 else "cpu")

    for text in texts[:50]:
        t = (text or "").strip()
        if len(t) < 10:
            continue
        inputs = tokenizer(
            t[:4000],
            return_tensors="pt",
            truncation=True,
            max_length=max_length,
            padding=True,
        )
        inputs = {k: v.to(device) for k, v in inputs.items()}
        model.to(device)
        with torch.no_grad():
            logits = model(**inputs).logits
            probs = F.softmax(logits, dim=-1)[0]
        p_pos = float(probs[pos_idx].item())
        p_neg = float(probs[neg_idx].item())
        scores.append(p_pos - p_neg)

    if not scores:
        return 0.0
    return sum(scores) / len(scores)
