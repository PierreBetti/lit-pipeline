"""
Semantic layer of lit-pipeline: runs a small open-source embedding model locally (inside GitHub Actions),
so it uses no AI quota and gives identical results on every run.

  - semantic match: how close a paper's meaning is to each of your research questions
  - semantic discovery: papers found by meaning (OpenAlex relevance search with your questions,
    re-ranked by the model), catching work that uses different vocabulary and is not linked by citations
  - text-based active learning: learns from the content of what you keep and exclude
  - stopping estimate: how many relevant papers are probably still in your screening queue

Everything degrades gracefully: if the model cannot be loaded, the pipeline runs as before.
"""

import hashlib
import math

SEM_DEFAULTS = {
    "enabled": True,
    "model": "BAAI/bge-small-en-v1.5",
    "query_prefix": "Represent this sentence for searching relevant passages: ",
    "cos_low": 0.62,             # cosine mapped to a Semantic match of 0 (calibrated on real runs)
    "cos_high": 0.84,            # cosine mapped to a Semantic match of 100
    "reserved_slots": 8,         # suggestions reserved for papers found by meaning
    "discovery_per_question": 60,
    "discovery_keep": 30,
}

_MODEL = {"obj": None, "tried": False}


def settings(config):
    return {**SEM_DEFAULTS, **(config.get("semantic") or {})}


class Embedder:
    """Thin wrapper so tests can swap in a fake model."""

    def __init__(self, model_name, query_prefix):
        from sentence_transformers import SentenceTransformer   # heavy import, done only when needed
        self.model = SentenceTransformer(model_name, device="cpu")
        self.name = model_name
        self.query_prefix = query_prefix

    def docs(self, texts):
        return self.model.encode(list(texts), batch_size=32, normalize_embeddings=True, show_progress_bar=False)

    def queries(self, texts):
        return self.model.encode([self.query_prefix + t for t in texts], normalize_embeddings=True,
                                 show_progress_bar=False)


def get_embedder(config, log):
    s = settings(config)
    if not s["enabled"]:
        return None
    if not _MODEL["tried"]:
        _MODEL["tried"] = True
        try:
            _MODEL["obj"] = Embedder(s["model"], s["query_prefix"])
            log(f"   semantic model loaded: {s['model']}")
        except Exception as e:     # missing package, no network for the first download, etc.
            log(f"   semantic model unavailable ({type(e).__name__}: {str(e)[:120]}); continuing without it")
    return _MODEL["obj"]


def paper_text(title, abstract):
    return (title or "").strip() + ". " + (abstract or "").strip()


def rq_descriptions(config, research_questions):
    """Full wording of each research question (config 'research_question_descriptions'), label as fallback."""
    desc = config.get("research_question_descriptions") or {}
    return [desc.get(q, q) for q in research_questions]


def rq_version(config, research_questions):
    return hashlib.md5("|".join(rq_descriptions(config, research_questions)).encode()).hexdigest()[:10]


def match_scores(emb, config, research_questions, texts):
    """Matrix [paper][question] of cosine similarities, and a 0-100 Semantic match per paper (best question)."""
    import numpy as np
    s = settings(config)
    if not texts:
        return np.zeros((0, len(research_questions))), []
    q = emb.queries(rq_descriptions(config, research_questions))
    d = emb.docs(texts)
    cos = d @ q.T
    best = cos.max(axis=1)
    scaled = [int(round(100 * min(1.0, max(0.0, (c - s["cos_low"]) / (s["cos_high"] - s["cos_low"]))))) for c in best]
    return cos, scaled


# ----------------------------------------------------------------------------
# Semantic discovery
# ----------------------------------------------------------------------------
def discover(emb, config, research_questions, openalex, abstract_text, short_id, exclude, log):
    """Search OpenAlex with each research question, re-rank everything by meaning, return {work_id: match}."""
    s = settings(config)
    pool = {}
    for desc in rq_descriptions(config, research_questions):
        res = openalex("/works", {"search": desc, "filter": "type:article|review", "per_page": s["discovery_per_question"],
                                  "select": "id,display_name,abstract_inverted_index"})
        for w in (res or {}).get("results", []):
            wid = short_id(w["id"])
            if wid not in exclude and wid not in pool:
                pool[wid] = paper_text(w.get("display_name"), abstract_text(w.get("abstract_inverted_index")))
    if not pool:
        return {}
    ids = list(pool)
    _, scaled = match_scores(emb, config, research_questions, [pool[i] for i in ids])
    ranked = sorted(zip(ids, scaled), key=lambda t: t[1], reverse=True)[: s["discovery_keep"]]
    log(f"   semantic discovery: {len(pool)} papers found by meaning, keeping the {len(ranked)} closest")
    return {wid: sc for wid, sc in ranked if sc > 0}


# ----------------------------------------------------------------------------
# Text-based active learning
# ----------------------------------------------------------------------------
def train_text_model(emb, positives, negatives, eval_items):
    """
    positives / negatives: lists of texts (library papers + kept suggestions / excluded suggestions).
    eval_items: list of (text, label) for screened suggestions only, used for the cross-validated AUC.
    Returns a dict with a predict(texts) function, or a 'not ready' dict.
    """
    if emb is None or len(negatives) < 3 or len(positives) < 3:
        return {"ready": False, "positives": len(positives), "negatives": len(negatives)}
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    X = emb.docs(positives + negatives)
    y = np.array([1] * len(positives) + [0] * len(negatives))
    clf = LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000).fit(X, y)

    # cross-validated AUC on the screened suggestions (library papers always stay in training)
    auc_cv = None
    if eval_items and len({l for _, l in eval_items}) == 2:
        texts = [t for t, _ in eval_items]
        labels = [l for _, l in eval_items]
        Xe = emb.docs(texts)
        folds = 5 if len(texts) >= 25 else len(texts)
        order = sorted(range(len(texts)), key=lambda i: hashlib.md5(str(i).encode()).hexdigest())
        n_lib = len(positives) - sum(labels)            # library positives are not in eval_items
        Xlib = X[:max(0, n_lib)]
        preds, truth = [], []
        for f in range(folds):
            test = [order[i] for i in range(len(order)) if i % folds == f]
            train = [i for i in range(len(texts)) if i not in test]
            Xt = np.vstack([Xlib] + [Xe[train]]) if len(train) else Xlib
            yt = np.array([1] * len(Xlib) + [labels[i] for i in train])
            if len(set(yt)) < 2:
                continue
            m = LogisticRegression(C=1.0, class_weight="balanced", max_iter=2000).fit(Xt, yt)
            preds += list(m.predict_proba(Xe[test])[:, 1])
            truth += [labels[i] for i in test]
        auc_cv = _auc(preds, truth)

    return {"ready": True, "positives": len(positives), "negatives": len(negatives), "auc_cv": auc_cv,
            "predict": lambda texts: list(clf.predict_proba(emb.docs(texts))[:, 1]) if texts else []}


def _auc(scores, labels):
    pos = [s for s, t in zip(scores, labels) if t]
    neg = [s for s, t in zip(scores, labels) if not t]
    if not pos or not neg:
        return None
    return round(sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg)), 3)


def stopping_estimate(probabilities):
    """Expected number of relevant papers still in the queue = sum of their predicted probabilities."""
    expected = sum(probabilities)
    # 90% upper bound from a Poisson-binomial normal approximation
    var = sum(p * (1 - p) for p in probabilities)
    upper = expected + 1.2816 * math.sqrt(var)
    return {"queue": len(probabilities), "expected_relevant": round(expected, 1), "upper_90": round(upper, 1)}
