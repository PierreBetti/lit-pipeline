# lit-pipeline: methods

A living, question-driven literature review system. This document describes every computation exactly as
implemented in `pipeline.py` and `validate.py`, as a basis for a methods paper.

## 1. Data sources

- **Library**: the researcher's Zotero library, synced to a Notion database by the Notero plugin.
- **Bibliographic data**: OpenAlex (CC0), queried by DOI (title search as a fallback, accepted only on an
  exact normalized title match). Citation counts, field- and year-normalized citation percentile, FWCI,
  reference lists, retraction flags.
- **Recommendations**: Semantic Scholar recommendations API (optional second signal).
- **Geocoding**: OpenStreetMap Nominatim, cached, one request per second.
- **Language model**: Google Gemini through its API (free tier by default), temperature 0, structured JSON
  output constrained by a schema with closed category lists. Claude is supported as an alternative.

## 2. Library enrichment

For each library paper: OpenAlex metrics; an AI triage from title and abstract (summary, relevance 1 to 5
against the researcher's written research context, research questions, ecosystem, gases, methods, key
result, subject category if empty); and a structured extraction (study sites, biome, study design,
duration, reported quantitative values). When the abstract synced from Zotero is missing or truncated, the
OpenAlex abstract is used instead.

Reading priority (Notion formula):
`Priority = 100 × (0.60·r/5 + 0.25·pct/100 + 0.15·rec)`, with `r` the AI relevance, `pct` the citation
percentile (0.5 if unknown) and `rec = max(0, 1 − (current year − publication year)/15)`.

## 3. Discovery of new papers

**Candidates.** Papers not in the library that (a) are cited by library papers, (b) cite library papers
(up to 2 × 200 citing papers per batch of 40 library papers, sorted by citations), (c) are co-cited with
library papers in those citing papers, or (d) are recommended by Semantic Scholar from the library DOIs.

**Pre-ranking.** `pre(X) = 2·|cited by library| + 2·|cites library| + Σ co-citations + 2·[Semantic Scholar]`.
The 150 best candidates with `pre ≥ 2` are scored in depth.

**Similarity.** For candidate X and library paper L, with R(·) the reference list:
- bibliographic coupling `BC = |R(X) ∩ R(L)| / √(|R(X)|·|R(L)|)`
- co-citation `CC = c(X,L) / √(a(X)·a(L))`, where `c(X,L)` counts sampled citing papers citing both, and
  `a(·)` counts sampled citing papers citing each
- direct link `D = 1` if X cites L or L cites X
- `sim(X,L) = 0.45·BC + 0.45·CC + 0.10·D`

Aggregated over the library and weighted by the researcher's own relevance ratings:
`S(X) = Σ_L (r_L/5)·sim(X,L)` (r_L = 3 when unrated), rescaled to 0 to 100 relative to the best candidate.

**Roles.** Prior work: cited by ≥ 2 library papers. Derivative work: cites ≥ 2 library papers. Similar work:
similarity ≥ 40 or no other role.

**Semantic match.** A local open-source sentence-embedding model (default BAAI/bge-small-en-v1.5, run on CPU
inside the workflow, so no AI quota and identical results across runs) embeds each research question (full
wording in `config.json`, with the model's query instruction) and each candidate's title and abstract. The
Semantic match M is the best cosine similarity across questions, mapped linearly from [0.62, 0.84] to [0, 100] (range calibrated on the first real runs, where cosines fell
between about 0.65 and 0.82). Pending suggestions are re-scored every run so the scale stays consistent.

**Semantic discovery.** Each research question is also sent to OpenAlex's relevance search (60 results per
question); the pooled results are re-ranked by Semantic match and the 30 closest join the candidate pool
(always scored in depth, source "Semantic search"). Because such papers have few citation links, and so a
low Similarity, 8 of the 40 proposed suggestions are reserved for the best of them. This reaches papers
that use different vocabulary and are not linked to the library by citations.

**Global score** (identical in Python and in Notion):
`G = 100 × (0.30·r/5 + 0.25·S/100 + 0.15·M/100 + 0.12·min(C,5)/5 + 0.10·pct/100 + 0.08·rec)`, where C is the
number of library papers directly linked (+1 for a Semantic Scholar recommendation); unknown r, M or pct count
as 0.5.
The top 40 candidates are proposed for screening and receive an AI relevance score (up to 20 per run).

**Text-based active learning.** A logistic regression (L2, C = 1, balanced classes) on the embeddings of
titles and abstracts, with every library paper and every retained suggestion as positives and every excluded
suggestion as negatives (needs ≥ 3 of each). Its cross-validated AUC is computed on screened suggestions
only (5 folds, leave-one-out below 25), with library papers kept in every training fold. The Personal score
is the mean of the text model and the feature model below, whichever are ready.

**Stopping estimate.** The expected number of relevant papers left in the screening queue is the sum of
the Personal-score probabilities of pending suggestions, with a 90% upper bound from the normal
approximation of the Poisson-binomial distribution. Below 1, further screening is unlikely to pay off.

**Feature-based preference learning.** Once ≥ 12 screening decisions exist (≥ 3 retained, ≥ 3 excluded), an L2-regularized
logistic regression (λ = 0.05, batch gradient descent) is fit on five features (AI relevance, similarity,
direct links, citation percentile, recency) to predict *retained vs excluded*. Its output is the Personal
score. Evaluation: in-sample AUC, 5-fold cross-validated AUC (leave-one-out below 25 decisions), and a
prospective AUC using only scores recorded before the decision was made.

## 4. Field-wide systematic map

**Scopes.** Nested OpenAlex boolean searches on titles and abstracts (`scopes.json`), in English and French,
over articles, reviews, theses, reports, book chapters and preprints, ordered from most specific to broadest. A scope with ≤ 4000 matches is retrieved in
full; a larger one is represented by a simple random sample of 1500 works drawn by OpenAlex with a fixed
seed (42). Full scopes are refreshed weekly, samples every 120 days or when the query changes.

**Classification.** Each work is classified once (innermost scope first, 200 per run): on-topic or not,
research questions, ecosystems, gases, biome, study design, named sites. Abstracts are fetched at
classification time and never stored.

**Estimation.** For scope s with N matches and n classified works of which m are on-topic:
on-topic rate `ρ = m/n`, estimated on-topic papers `N_on = N·ρ`. For a category k observed in `x_k` of the
m on-topic works: `p_k = x_k/m` with a Wilson 95% interval; in sampled scopes the interval includes a finite
population correction (z scaled by `√((N_on − m)/(N_on − 1))`). Estimated count `N_on·p_k`.

**Geography.** Sites are geocoded with OpenStreetMap Nominatim (English place names), which also returns
the country code and province or state; a work belongs to a geographic level (`config.json`, `geography`:
by default World, Canada, Québec) if any of its located sites falls within it. Below the world level,
statistics are computed on located works only, and the share of on-topic works that could be located is
reported alongside. In sampled scopes, counts are scaled by the same sampling factor as the scope. A drop-off
is flagged when a research question's share at a level is less than half its worldwide share (for questions
with at least 5% worldwide), and a geographic gap when no located work addresses it.

**Gaps.** A cell (ecosystem × research question) is a *research gap* when its estimated count in the field
is below max(3, 2% of N_on), and a *reading gap* when the field is above that threshold but the library has
no paper in the cell. In the rings figure, a sector is a gap when `p_k < 3%` or its estimate is below 3
(shown only once ≥ 20 on-topic works are classified).

## 5. Weekly digest

Every 7 days a Notion page summarizes: the best new suggestions (by AI relevance, then Personal score, then
Semantic match), papers added to the library, ecosystem × research question cells newly covered by the
library, field map progress, the stopping estimate, and alerts (retractions, duplicates).

## 6. Reproducibility

- Temperature 0; the exact model version returned by the API is recorded for every answer.
- Every prompt (system text + output schema) is hashed; all prompt versions are archived in
  `data/state.json` and each classification stores its prompt hash.
- Every AI answer is appended to `data/ai_log.jsonl` (date, task, item, model version, prompt hash, output).
- Each run is logged (Notion Review log, `logs/review_log.csv`); every screening decision with its date
  (`logs/decisions.csv`); the full history is versioned in git.
- `config.json` can pin a single model version for a study (`ai.pinned_model`).
- The classified field corpus is exported nightly to `export/field_corpus.csv` (no abstracts).

## 7. Validation protocol (`validate.py`)

1. **AI vs human classification.** Random sample of 50 classified works per scope; 30 double-coded.
   Coding is blind (AI labels are not shown in Notion). Metrics: Cohen's kappa (binary and single-choice
   categories), precision, recall, F1 per label, micro-F1 per multi-label category; human-human agreement
   on the double-coded subset as a reference ceiling; on-topic accuracy with vs without abstract.
2. **Calibration of estimates.** On fully classified scopes (≥ 150 works), repeated random sub-samples
   (n = 50, 100, 200, 400; 400 repetitions) test whether the 95% intervals contain the true proportion
   95% of the time; mean absolute error in percentage points.
3. **Test-retest consistency.** 30 random works re-classified; agreement with the first classification.
4. **Preference model.** Cross-validated and prospective AUC (section 3).
5. **Discovery recall.** Ground truth: the included studies of published systematic reviews
   (`benchmarks/*.json`). Seeded with k = 5 random included studies (3 repetitions), the researcher is
   simulated: each round, the top 50 unscreened candidates are screened and any included study found joins
   the library before the next round (3 rounds). Reported: recall and number screened per round, against
   one round of plain citation chasing (all references and citing papers of the seeds, unranked), and
   against other tools run manually with the same seeds.

## 8. Known limitations

OpenAlex coverage of grey literature, non-English work, abstracts (some publishers withhold them, so a
share of works is classified from titles only, reported separately) and reference lists; English-only
search queries; dependence on a commercial language model whose versions change (mitigated by version
logging and pinning); the AI ranks and classifies but every inclusion decision remains human.
