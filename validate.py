#!/usr/bin/env python3
"""
Validation toolkit for lit-pipeline. Produces the numbers a methods paper needs.

  python validate.py sample        draw a blind, stratified sample of field papers into the Notion
                                   "Validation sample" database for hand-coding
  python validate.py evaluate      AI vs human agreement on the coded rows (kappa, precision, recall, F1),
                                   and human vs human agreement on the double-coded subset
  python validate.py calibration   do the 95% confidence intervals of sampled scopes really contain the
                                   truth 95% of the time? (simulated on fully classified scopes, no AI needed)
  python validate.py consistency   classify the same papers again and measure test-retest agreement of the AI
  python validate.py benchmark     recall of the discovery algorithm against published systematic reviews
                                   (benchmarks/*.json), compared with plain citation chasing
  python validate.py all           everything that can run with the data available

Results go to data/validation.json and the dashboard's Validation tab.
"""

import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pipeline as P

ROOT = Path(__file__).resolve().parent
RESULTS = P.DATA_DIR / "validation.json"
SAMPLE_FILE = P.DATA_DIR / "validation_sample.json"
VCFG = {"per_scope": 50, "double_coded": 30, "consistency_n": 30, "seed": 2026,
        **P.CONFIG.get("validation", {})}


# ----------------------------------------------------------------------------
# Agreement statistics
# ----------------------------------------------------------------------------
def kappa(a, b):
    """Cohen's kappa for two equal-length lists of labels (any hashable values)."""
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    cats = set(a) | set(b)
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in cats)
    return None if pe == 1 else round((po - pe) / (1 - pe), 3)


def binary_metrics(pred, truth):
    tp = sum(p and t for p, t in zip(pred, truth))
    fp = sum(p and not t for p, t in zip(pred, truth))
    fn = sum(t and not p for p, t in zip(pred, truth))
    n = len(pred)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec == 0 or rec == 0 else None)
    return {"n": n, "positives": sum(truth), "accuracy": round(sum(p == t for p, t in zip(pred, truth)) / n, 3) if n else None,
            "precision": None if prec is None else round(prec, 3), "recall": None if rec is None else round(rec, 3),
            "f1": None if f1 is None else round(f1, 3), "kappa": kappa(list(pred), list(truth))}


def compare(rows_a, rows_b):
    """Compare two codings of the same papers (lists of dicts with on_topic, rqs, ecosystems, gases, biome, design)."""
    out = {"n": len(rows_a)}
    out["on_topic"] = binary_metrics([r["on_topic"] for r in rows_a], [r["on_topic"] for r in rows_b])
    for field, options in (("rqs", P.RESEARCH_QUESTIONS), ("ecosystems", P.ECOSYSTEMS), ("gases", P.GASES)):
        per = {o: binary_metrics([o in r[field] for r in rows_a], [o in r[field] for r in rows_b]) for o in options}
        tp = sum(len(set(x[field]) & set(y[field])) for x, y in zip(rows_a, rows_b))
        fp = sum(len(set(x[field]) - set(y[field])) for x, y in zip(rows_a, rows_b))
        fn = sum(len(set(y[field]) - set(x[field])) for x, y in zip(rows_a, rows_b))
        micro = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None
        kappas = [m["kappa"] for m in per.values() if m["kappa"] is not None]
        out[field] = {"per_label": per, "micro_f1": None if micro is None else round(micro, 3),
                      "mean_kappa": round(statistics.mean(kappas), 3) if kappas else None}
    for field in ("biome", "design"):
        pairs = [(x[field], y[field]) for x, y in zip(rows_a, rows_b) if y[field]]
        out[field] = {"n": len(pairs), "accuracy": round(sum(p == q for p, q in pairs) / len(pairs), 3) if pairs else None,
                      "kappa": kappa([p for p, _ in pairs], [q for _, q in pairs])}
    return out


def ai_labels(k):
    return {"on_topic": bool(k["o"]), "rqs": k["q"], "ecosystems": k["e"], "gases": k["g"],
            "biome": k["b"], "design": k["z"]}


# ----------------------------------------------------------------------------
# 1. Blind sample for hand-coding
# ----------------------------------------------------------------------------
def task_sample():
    if not P.VALIDATION_DS:
        sys.exit("No validation_data_source in config.json")
    corpus = P.load_corpus()
    cfg = P.load_scopes()
    taken = set(json.loads(SAMPLE_FILE.read_text())["ids"]) if SAMPLE_FILE.exists() else set()
    rng = random.Random(VCFG["seed"] + len(taken))
    chosen = []
    for scope in cfg["scopes"]:
        pool = sorted(wid for wid, r in corpus.items()
                      if scope["id"] in r["s"] and "k" in r and wid not in taken and wid not in chosen)
        pick = rng.sample(pool, min(VCFG["per_scope"], len(pool)))
        chosen += [(wid, scope["label"]) for wid in pick]
    if not chosen:
        print("Nothing to sample yet: the field map has no classified papers. Run the nightly workflow first.")
        return
    abstracts = P.fetch_abstracts([wid for wid, _ in chosen])
    double = set(rng.sample([wid for wid, _ in chosen], min(VCFG["double_coded"], len(chosen))))
    for wid, label in chosen:
        r = corpus[wid]
        for coder in (["A", "B"] if wid in double else ["A"]):
            P.create_page(P.VALIDATION_DS, {
                "Title": P.p_title(r["t"]), "Sample ID": P.p_text(wid), "Coder": P.p_select(coder),
                "Scope": P.p_select(label), "Year": P.p_num(r.get("y")), "DOI": P.p_url(r.get("d")),
                "Abstract": P.p_text(abstracts.get(wid) or "(no abstract available: code from the title)"),
            })
    P.DATA_DIR.mkdir(exist_ok=True)
    SAMPLE_FILE.write_text(json.dumps({"ids": sorted(taken | {w for w, _ in chosen}), "double": sorted(double)}))
    print(f"Added {len(chosen)} papers ({len(double)} double-coded) to the Validation sample database.")


# ----------------------------------------------------------------------------
# 2. AI vs human, human vs human
# ----------------------------------------------------------------------------
def task_evaluate():
    corpus = P.load_corpus()
    rows = P.query_all(P.VALIDATION_DS)
    human = defaultdict(dict)
    for page in rows:
        pr = page["properties"]
        if not (pr.get("Coded") or {}).get("checkbox"):
            continue
        wid = P.read(pr.get("Sample ID"))
        human[wid][P.read(pr.get("Coder")) or "A"] = {
            "on_topic": bool((pr.get("On topic") or {}).get("checkbox")),
            "rqs": P.read(pr.get("Research questions")) or [], "ecosystems": P.read(pr.get("Ecosystem")) or [],
            "gases": P.read(pr.get("Gases")) or [], "biome": P.read(pr.get("Biome")), "design": P.read(pr.get("Study design")),
        }
    ids = [w for w in human if "A" in human[w] and "k" in corpus.get(w, {})]
    if not ids:
        return {"status": "waiting", "message": "No coded rows yet in the Validation sample database."}
    ai = [ai_labels(corpus[w]["k"]) for w in ids]
    hu = [human[w]["A"] for w in ids]
    res = {"status": "ok", "coded": len(ids), "ai_vs_human": compare(ai, hu)}
    with_abs = [i for i, w in enumerate(ids) if not corpus[w]["k"].get("na")]
    title_only = [i for i, w in enumerate(ids) if corpus[w]["k"].get("na")]
    for name, idx in (("with_abstract", with_abs), ("title_only", title_only)):
        if idx:
            res[name] = binary_metrics([ai[i]["on_topic"] for i in idx], [hu[i]["on_topic"] for i in idx])
    both = [w for w in ids if "B" in human[w]]
    if both:
        res["human_vs_human"] = compare([human[w]["A"] for w in both], [human[w]["B"] for w in both])
    return res


# ----------------------------------------------------------------------------
# 3. Calibration of the sampling estimates
# ----------------------------------------------------------------------------
def task_calibration(repeats=400):
    corpus = P.load_corpus()
    state = P.load_state()
    rng = random.Random(VCFG["seed"])
    out = []
    for sid, meta in state.get("field_scopes", {}).items():
        recs = [r for r in corpus.values() if sid in r["s"] and "k" in r]
        if meta.get("mode") != "full" or len(recs) < 150 or len(recs) < 0.95 * meta.get("fetched", 1e9):
            continue
        on = [r for r in recs if r["k"]["o"]]
        n_on = len(on)
        truth = {q: sum(q in r["k"]["q"] for r in on) / len(on) for q in P.RESEARCH_QUESTIONS}
        sizes = [n for n in (50, 100, 200, 400) if n < len(recs)]
        res = {"scope": meta.get("label", sid), "population": len(recs), "sizes": {}}
        for n in sizes:
            hits = total = 0
            errs = []
            for _ in range(repeats):
                samp = [r for r in rng.sample(recs, n) if r["k"]["o"]]
                for q in P.RESEARCH_QUESTIONS:
                    k_ = sum(q in r["k"]["q"] for r in samp)
                    p, lo, hi = P.wilson(k_, len(samp), population=n_on)
                    hits += lo <= truth[q] <= hi
                    total += 1
                    errs.append(abs(p - truth[q]))
            res["sizes"][n] = {"coverage": round(hits / total, 3), "mean_abs_error_pts": round(100 * statistics.mean(errs), 2)}
        out.append(res)
    if not out:
        return {"status": "waiting", "message": "Needs a fully classified scope of at least 150 papers."}
    return {"status": "ok", "nominal": 0.95, "repeats": repeats, "scopes": out}


# ----------------------------------------------------------------------------
# 4. Test-retest consistency of the AI
# ----------------------------------------------------------------------------
def task_consistency():
    corpus = P.load_corpus()
    triager = P.Triager()
    if not triager.available:
        return {"status": "waiting", "message": "No AI key available."}
    pool = sorted(w for w, r in corpus.items() if "k" in r)
    if len(pool) < 10:
        return {"status": "waiting", "message": "Not enough classified field papers yet."}
    pick = random.Random(VCFG["seed"] + len(pool)).sample(pool, min(VCFG["consistency_n"], len(pool)))
    abstracts = P.fetch_abstracts(pick)
    first, second = [], []
    for wid in pick:
        r = corpus[wid]
        res = triager.run(r["t"][:80], r["t"], r.get("y"), "", abstracts.get(wid, ""), kind="field")
        if triager.exhausted:
            break
        if not res:
            continue
        first.append(ai_labels(r["k"]))
        second.append({"on_topic": bool(res.get("on_topic")), "rqs": P.keep(res.get("research_questions"), P.RESEARCH_QUESTIONS),
                       "ecosystems": P.keep(res.get("ecosystems"), P.ECOSYSTEMS), "gases": P.keep(res.get("gases"), P.GASES),
                       "biome": res.get("biome"), "design": res.get("study_design")})
    if not first:
        return {"status": "waiting", "message": "The AI quota was used up; try again tomorrow."}
    exact_rq = sum(set(a["rqs"]) == set(b["rqs"]) for a, b in zip(first, second)) / len(first)
    return {"status": "ok", "n": len(first), "model": triager.model, "exact_rq_match": round(exact_rq, 3),
            "agreement": compare(second, first)}


# ----------------------------------------------------------------------------
# 5. Preference learning (cross-validated and prospective)
# ----------------------------------------------------------------------------
def task_preferences():
    try:
        existing = P.load_existing_suggestions()
    except RuntimeError as e:
        return {"status": "waiting", "message": str(e)[:150]}
    model = P.train_preferences(existing)
    state = P.load_state()
    return {"status": "ok" if model.get("ready") else "waiting", "n": model["n"], "auc_in_sample": model.get("auc"),
            "auc_cross_validated": model.get("auc_cv"), "prospective": P.prospective_eval(existing, state),
            "message": None if model.get("ready") else f"Needs {model['needed']} screening decisions."}


# ----------------------------------------------------------------------------
# 6. Recall benchmark against published systematic reviews
# ----------------------------------------------------------------------------
def task_benchmark():
    files = sorted((ROOT / "benchmarks").glob("*.json"))
    files = [f for f in files if not f.name.startswith("_")]
    if not files:
        return {"status": "waiting", "message": "Add benchmarks/<name>.json files (see benchmarks/_TEMPLATE.json)."}
    out = []
    for f in files:
        spec = json.loads(f.read_text(encoding="utf-8"))
        k, repeats = spec.get("seeds", 5), spec.get("repeats", 3)
        per_round, rounds = spec.get("screen_per_round", 50), spec.get("rounds", 3)
        gold = {}
        for doi in spec["gold_dois"]:
            d = P.norm_doi(doi)
            w = P.work_by_doi(d) if d else None
            if w:
                gold[P.short_id(w["id"])] = w
        print(f"{spec['name']}: {len(gold)}/{len(spec['gold_dois'])} gold papers found in OpenAlex")
        if len(gold) < k + 3:
            out.append({"name": spec["name"], "status": "too few gold papers in OpenAlex", "findable": len(gold)})
            continue
        curves, base_recall, base_load = [], [], []
        for rep in range(repeats):
            rng = random.Random(1000 + rep)
            seeds = rng.sample(sorted(gold), k)
            found, screened, curve = set(seeds), set(), []
            for _ in range(rounds):
                lib = [{"name": wid, "title": gold[wid].get("display_name", ""), "doi": P.norm_doi(gold[wid].get("doi")),
                        "work": gold[wid], "relevance": None} for wid in sorted(found)]
                ranked, _, _, _ = P.build_suggestions(lib, {}, keep_n=P.CANDIDATE_POOL)
                batch = [c["id"] for c in ranked if c["id"] not in screened][:per_round]
                screened |= set(batch)
                found |= {c for c in batch if c in gold}
                curve.append({"screened": len(screened), "recall": (len(found) - k) / (len(gold) - k)})
            curves.append(curve)
            # baseline: one round of plain citation chasing (all references + all citing papers, unranked)
            refs = {P.short_id(r) for s in seeds for r in (gold[s].get("referenced_works") or [])}
            cands = (refs | set(P.fetch_citers(set(seeds)))) - set(seeds)
            base_recall.append(len(cands & set(gold)) / (len(gold) - k))
            base_load.append(len(cands))
        out.append({
            "name": spec["name"], "source": spec.get("source", ""), "gold": len(spec["gold_dois"]), "findable": len(gold),
            "seeds": k, "repeats": repeats, "status": "ok",
            "rounds": [{"round": i + 1, "screened": round(statistics.mean(c[i]["screened"] for c in curves)),
                        "recall": round(statistics.mean(c[i]["recall"] for c in curves), 3),
                        "recall_sd": round(statistics.pstdev(c[i]["recall"] for c in curves), 3)} for i in range(rounds)],
            "baseline": {"recall": round(statistics.mean(base_recall), 3), "screened": round(statistics.mean(base_load))},
            "comparators": spec.get("comparators", {}),
        })
    return {"status": "ok", "benchmarks": out}


# ----------------------------------------------------------------------------
def save(results):
    P.DATA_DIR.mkdir(exist_ok=True)
    current = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    current.update(results)
    current["updated"] = P.TODAY
    RESULTS.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
    dash = P.DATA_DIR / "dashboard.json"
    if dash.exists():   # refresh the dashboard's Validation tab right away
        data = json.loads(dash.read_text(encoding="utf-8"))
        data["validation"] = current
        P.write_dashboard(data)
    print(json.dumps(results, indent=1)[:3000])


def main():
    task = sys.argv[1] if len(sys.argv) > 1 else "all"
    if task == "sample":
        task_sample()
        return
    runners = {"evaluate": task_evaluate, "calibration": task_calibration, "consistency": task_consistency,
               "preferences": task_preferences, "benchmark": task_benchmark}
    chosen = list(runners) if task == "all" else [task]
    results = {}
    for name in chosen:
        print(f"== {name}")
        try:
            results[name] = runners[name]()
        except Exception as e:      # one failing task should not hide the others
            results[name] = {"status": "error", "message": f"{type(e).__name__}: {e}"[:300]}
    save(results)


if __name__ == "__main__":
    main()
