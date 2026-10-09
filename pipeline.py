#!/usr/bin/env python3
"""
lit-pipeline: nightly enrichment of the Notero (Zotero -> Notion) database.

  1. Metrics      citation count, field-normalized citation percentile and FWCI (OpenAlex)
  2. Triage       AI summary, relevance to the PhD, research questions, ecosystem, gases,
                  methods, key result, and Category when it is still empty
                  (Gemini API free tier by default)
  3. Suggestions  papers strongly connected to your library but not in it yet
                  (OpenAlex citation links + Semantic Scholar recommendations)
  4. Extraction   study sites (geocoded), biome, study design, duration, reported values
  5. Dashboard    docs/index.html with tabs: citation map, study sites, evidence gaps,
                  timeline, screening (PRISMA flow, saturation curve, audit log)
  Every run is logged to the Notion Review log, logs/*.csv and data/state.json (audit trail).

Usage:
  python pipeline.py              full run
  python pipeline.py --no-ai      skip the AI triage step
  python pipeline.py --dry-run    read and compute everything, write nothing to Notion
"""

import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent

# ----------------------------------------------------------------------------
# Configuration (secrets come from environment variables / GitHub secrets)
# ----------------------------------------------------------------------------
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
OPENALEX_KEY = os.environ.get("OPENALEX_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")          # free tier, default AI provider
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")    # optional paid alternative
S2_KEY = os.environ.get("S2_API_KEY", "")  # optional, Semantic Scholar works without it
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")
# Tried in order. A model whose free quota is 0 for your key is skipped automatically.
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODELS", "gemini-2.5-flash-lite,gemini-2.5-flash,gemini-flash-lite-latest,gemini-flash-latest").split(",") if m.strip()]
GEMINI_MODEL = GEMINI_MODELS[0]
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
GEMINI_SECONDS_BETWEEN_CALLS = float(os.environ.get("GEMINI_SECONDS_BETWEEN_CALLS", "7"))  # stays under free-tier per-minute limits

LIBRARY_DS = os.environ.get("NOTERO_DATA_SOURCE_ID", "87a4954a-4928-8345-b3a8-87caeb0b2c4a")
SUGGEST_DS = os.environ.get("SUGGESTED_DATA_SOURCE_ID", "444be872-ca23-4e7e-8d93-665568721b36")
REVIEW_LOG_DS = os.environ.get("REVIEW_LOG_DATA_SOURCE_ID", "4e18753f-4ddc-4caa-911c-04eacc6a4d90")


MAX_TRIAGE_PER_RUN = int(os.environ.get("MAX_TRIAGE_PER_RUN", "40"))
MAX_SUGGESTIONS = int(os.environ.get("MAX_SUGGESTIONS", "40"))
MIN_CONNECTIONS = int(os.environ.get("MIN_CONNECTIONS", "2"))
GRAPH_SUGGESTIONS = int(os.environ.get("GRAPH_SUGGESTIONS", "25"))
MAX_SUGGESTION_TRIAGE_PER_RUN = int(os.environ.get("MAX_SUGGESTION_TRIAGE_PER_RUN", "20"))
CANDIDATE_POOL = int(os.environ.get("CANDIDATE_POOL", "150"))   # candidates scored in depth each run
CITER_PAGES = int(os.environ.get("CITER_PAGES", "2"))           # pages of 200 citing papers per batch
MAX_EXTRACT_PER_RUN = int(os.environ.get("MAX_EXTRACT_PER_RUN", "25"))
MIN_DECISIONS_TO_LEARN = int(os.environ.get("MIN_DECISIONS_TO_LEARN", "12"))
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"

DRY_RUN = "--dry-run" in sys.argv
# Gemini (free) is used when its key is set; Claude only if you choose to add a paid key instead.
AI_PROVIDER = "none" if "--no-ai" in sys.argv else ("gemini" if GEMINI_KEY else ("claude" if ANTHROPIC_KEY else "none"))
TODAY = dt.date.today().isoformat()

# Option lists must match the Notion property options exactly.
CATEGORIES = [
    "Fluxes & microclimate",
    "Vegetation & tree stems",
    "Soil & water microbes",
    "Hydrology & biogeochemistry",
    "Methods & instrumentation",
    "Nature-based solutions & policy",
]
RESEARCH_QUESTIONS = [
    "RQ1 Source/sink controls",
    "RQ2 Disturbed vs intact",
    "RQ3 Anthropogenic impact",
    "RQ4 Soil vs water C sources",
    "RQ5 Microbial taxonomy & function",
]
ECOSYSTEMS = ["Forested swamp", "Peatland / bog / fen", "Marsh", "Upland forest", "Other wetland", "Other"]
GASES = ["CO2", "CH4", "N2O"]
BIOMES = ["Boreal", "Temperate", "Subtropical", "Tropical", "Arctic / tundra", "Multiple / global", "Not stated"]
STUDY_DESIGNS = ["Field observation", "Field experiment", "Lab / incubation", "Modelling", "Review",
                 "Meta-analysis / synthesis", "Methods"]
SKIP_WORK_TYPES = {"paratext", "erratum", "retraction", "editorial", "letter", "peer-review"}

# ---- config.json overrides everything above (lists, Notion IDs, AI settings) --------
CONFIG = {}
if (ROOT / "config.json").exists():
    CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    CATEGORIES = CONFIG.get("categories", CATEGORIES)
    RESEARCH_QUESTIONS = CONFIG.get("research_questions", RESEARCH_QUESTIONS)
    ECOSYSTEMS = CONFIG.get("ecosystems", ECOSYSTEMS)
    GASES = CONFIG.get("gases", GASES)
    BIOMES = CONFIG.get("biomes", BIOMES)
    STUDY_DESIGNS = CONFIG.get("study_designs", STUDY_DESIGNS)
    _n = CONFIG.get("notion", {})
    LIBRARY_DS = os.environ.get("NOTERO_DATA_SOURCE_ID") or _n.get("library_data_source", LIBRARY_DS)
    SUGGEST_DS = os.environ.get("SUGGESTED_DATA_SOURCE_ID") or _n.get("suggestions_data_source", SUGGEST_DS)
    REVIEW_LOG_DS = os.environ.get("REVIEW_LOG_DATA_SOURCE_ID") or _n.get("review_log_data_source", REVIEW_LOG_DS)
AI_CFG = {"temperature": 0, "pinned_model": None, "log_raw_outputs": True, **CONFIG.get("ai", {})}
if AI_CFG.get("pinned_model"):
    GEMINI_MODELS = [AI_CFG["pinned_model"]]
elif AI_CFG.get("fallback_models") and "GEMINI_MODELS" not in os.environ:
    GEMINI_MODELS = list(AI_CFG["fallback_models"])
GEMINI_MODEL = GEMINI_MODELS[0]
VALIDATION_DS = CONFIG.get("notion", {}).get("validation_data_source", "")
DIGEST_DS = CONFIG.get("notion", {}).get("digest_data_source", "")


def prompt_hash(system, tool):
    return hashlib.sha256((system + json.dumps(tool, sort_keys=True)).encode()).hexdigest()[:12]


_PROMPTS_SEEN = {}
AI_LOG_FILE = "ai_log.jsonl"          # validate.py writes to its own file so the two workflows never collide


def log_ai_call(kind, label, model_version, phash, result):
    """Raw record of every AI answer: what was asked (prompt version), which exact model answered, what it said."""
    if DRY_RUN or not AI_CFG.get("log_raw_outputs", True):
        return
    DATA_DIR.mkdir(exist_ok=True)
    with (DATA_DIR / AI_LOG_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"date": TODAY, "kind": kind, "item": label, "model": model_version,
                            "prompt": phash, "output": result}, ensure_ascii=False) + "\n")


def log(*args):
    print(*args, flush=True)


# ----------------------------------------------------------------------------
# Notion
# ----------------------------------------------------------------------------
NOTION_API = "https://api.notion.com/v1"


def notion(method, path, body=None):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2025-09-03",
        "Content-Type": "application/json",
    }
    for attempt in range(6):
        r = requests.request(method, f"{NOTION_API}{path}", headers=headers, json=body, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
            continue
        if not r.ok:
            raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text[:600]}")
        time.sleep(0.34)  # Notion allows about 3 requests per second
        return r.json()
    raise RuntimeError(f"Notion {method} {path}: too many retries")


def query_all(data_source_id):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion("POST", f"/data_sources/{data_source_id}/query", body)
        pages.extend(res["results"])
        if not res.get("has_more"):
            return pages
        cursor = res["next_cursor"]


def update_page(page_id, props):
    if DRY_RUN:
        log(f"   [dry-run] would update {page_id}: {sorted(props)}")
        return
    notion("PATCH", f"/pages/{page_id}", {"properties": props})


def create_page(data_source_id, props):
    if DRY_RUN:
        log(f"   [dry-run] would create page in {data_source_id}")
        return None
    res = notion("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": data_source_id},
                                    "properties": props})
    return res.get("id")


def read(prop):
    """Turn a Notion property value into a plain Python value."""
    if not prop:
        return None
    t = prop["type"]
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in prop[t])
    if t in ("url", "number"):
        return prop[t]
    if t == "multi_select":
        return [o["name"] for o in prop["multi_select"]]
    if t in ("select", "status"):
        return prop[t]["name"] if prop[t] else None
    if t == "date":
        return prop["date"]["start"] if prop["date"] else None
    if t == "rollup":
        r = prop["rollup"]
        return r.get("number") if r.get("type") == "number" else None
    if t == "formula":
        f = prop["formula"]
        return f.get(f.get("type"))
    return None


def p_text(text):
    text = (text or "").strip()[:1990]
    return {"rich_text": [{"type": "text", "text": {"content": text}}] if text else []}


def p_title(text):
    return {"title": [{"type": "text", "text": {"content": (text or "Untitled")[:1990]}}]}


def p_num(x):
    return {"number": x}


def p_url(u):
    return {"url": u or None}


def p_multi(names):
    return {"multi_select": [{"name": n} for n in names]}


def p_select(name):
    return {"select": {"name": name} if name else None}


def p_date(d):
    return {"date": {"start": d} if d else None}


# ----------------------------------------------------------------------------
# OpenAlex
# ----------------------------------------------------------------------------
OPENALEX = "https://api.openalex.org"
WORK_FIELDS = ("id,doi,display_name,publication_year,type,cited_by_count,fwci,is_retracted,"
               "citation_normalized_percentile,referenced_works,authorships,"
               "primary_location,best_oa_location,abstract_inverted_index")


def openalex(path, params=None):
    params = dict(params or {})
    if OPENALEX_KEY:
        params["api_key"] = OPENALEX_KEY
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL
    for attempt in range(5):
        r = requests.get(f"{OPENALEX}{path}", params=params, timeout=60)
        if r.status_code == 404:
            return None
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if not r.ok:
            log(f"   OpenAlex {path} -> {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    return None


def short_id(openalex_id):
    return openalex_id.rsplit("/", 1)[-1] if openalex_id else None


def norm_doi(value):
    if not value:
        return None
    m = re.search(r"10\.\d{4,9}/[^\s\"<>]+", value)
    return m.group(0).rstrip(".").lower() if m else None


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def work_by_doi(doi):
    return openalex(f"/works/doi:{requests.utils.quote(doi, safe='/:')}", {"select": WORK_FIELDS})


def work_by_title(title):
    res = openalex("/works", {"search": title, "per_page": 5, "select": WORK_FIELDS})
    for w in (res or {}).get("results", []):
        if norm_title(w.get("display_name")) == norm_title(title):
            return w
    return None


def works_filter(filter_str, per_page=200, sort=None, select=WORK_FIELDS):
    params = {"filter": filter_str, "per_page": per_page, "select": select}
    if sort:
        params["sort"] = sort
    res = openalex("/works", params)
    return (res or {}).get("results", [])


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def abstract_text(inverted_index):
    if not inverted_index:
        return ""
    positions = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def first_author(work):
    auths = work.get("authorships") or []
    if not auths:
        return "Unknown"
    name = (auths[0].get("author") or {}).get("display_name") or "Unknown"
    return name.split()[-1]


def author_list(work, limit=6):
    names = [(a.get("author") or {}).get("display_name") for a in (work.get("authorships") or [])]
    names = [n for n in names if n]
    return ", ".join(names[:limit]) + (" et al." if len(names) > limit else "")


def journal(work):
    src = ((work.get("primary_location") or {}).get("source") or {})
    return src.get("display_name") or ""


def percentile(work):
    v = (work.get("citation_normalized_percentile") or {}).get("value")
    return round(v * 100, 1) if v is not None else None


def best_abstract(notion_abstract, openalex_abstract):
    """Notero sometimes syncs a truncated abstract. Use OpenAlex's version when it is clearly fuller."""
    a, b = (notion_abstract or "").strip(), (openalex_abstract or "").strip()
    if not a:
        return b
    if not b:
        return a
    looks_cut = not a.rstrip().endswith((".", "!", "?", ")", "]", '"'))
    if len(b) > len(a) + 150 or (looks_cut and len(b) > len(a)):
        return b
    return a


# ----------------------------------------------------------------------------
# Step 0: load the library
# ----------------------------------------------------------------------------
def load_library():
    papers = []
    for page in query_all(LIBRARY_DS):
        pr = page["properties"]
        papers.append({
            "page_id": page["id"],
            "notion_url": page.get("url"),
            "name": read(pr.get("Name")) or "Untitled",
            "title": read(pr.get("Title")) or read(pr.get("Name")) or "",
            "abstract": read(pr.get("Abstract")) or "",
            "journal": read(pr.get("Publication")) or "",
            "doi": norm_doi(read(pr.get("DOI"))),
            "year": read(pr.get("Year")),
            "status": read(pr.get("Reading status")),
            "category": read(pr.get("Category")) or [],
            "triage_date": read(pr.get("Triage date")),
            "relevance": read(pr.get("Relevance")),
            "summary": read(pr.get("Summary")) or "",
            "key_result": read(pr.get("Key result")) or "",
            "rqs": read(pr.get("Research questions")) or [],
            "ecosystems": read(pr.get("Ecosystem")) or [],
            "gases": read(pr.get("Gases")) or [],
            "biome": read(pr.get("Biome")),
            "design": read(pr.get("Study design")),
            "sites_text": read(pr.get("Study sites")) or "",
            "extraction_date": read(pr.get("Extraction date")),
            "retracted": bool((pr.get("Retracted") or {}).get("checkbox")),
            "citations": read(pr.get("Citations")),
            "percentile": read(pr.get("Citation percentile")),
            "fwci": read(pr.get("FWCI")),
            "openalex_url": read(pr.get("OpenAlex ID")),
            "zotero_uri": read(pr.get("Zotero URI")),
            "tier": read(pr.get("Tier")),
            "findings_count": read(pr.get("Findings count")) or 0,
            "drafts_count": read(pr.get("Drafts count")) or 0,
        })
    return papers


# ----------------------------------------------------------------------------
# Step 1: metrics
# ----------------------------------------------------------------------------
def enrich_metrics(papers):
    """Look every paper up on OpenAlex, write metrics back when they changed."""
    found = 0
    for p in papers:
        w = work_by_doi(p["doi"]) if p["doi"] else None
        if w is None and p["title"]:
            w = work_by_title(p["title"])
        if not w:
            log(f"   not found on OpenAlex: {p['name']}")
            continue
        found += 1
        p["work"] = w
        p["abstract"] = best_abstract(p["abstract"], abstract_text(w.get("abstract_inverted_index")))

        new = {
            "Citations": w.get("cited_by_count"),
            "Citation percentile": percentile(w),
            "FWCI": round(w["fwci"], 2) if w.get("fwci") is not None else None,
        }
        old = {"Citations": p["citations"], "Citation percentile": p["percentile"], "FWCI": p["fwci"]}
        retracted = bool(w.get("is_retracted"))
        if retracted and not p["retracted"]:
            log(f"   ⚠️ RETRACTED according to OpenAlex: {p['name']}")
        if new != old or p["openalex_url"] != w["id"] or retracted != p["retracted"]:
            props = {k: p_num(v) for k, v in new.items()}
            props["OpenAlex ID"] = p_url(w["id"])
            props["Retracted"] = {"checkbox": retracted}
            update_page(p["page_id"], props)
        p["retracted"] = retracted
    log(f"   {found}/{len(papers)} papers matched on OpenAlex")
    by_work = defaultdict(list)
    for p in papers:
        if p.get("work"):
            by_work[short_id(p["work"]["id"])].append(p["name"])
    duplicates = [names[0] for names in by_work.values() if len(names) > 1]
    for d in duplicates:
        log(f"   duplicate in your library (same paper twice): {d}. Merge it in Zotero.")
    return {"retractions": [p["name"] for p in papers if p["retracted"]], "duplicates": duplicates}


# ----------------------------------------------------------------------------
# Step 2: AI triage
# ----------------------------------------------------------------------------
TRIAGE_TOOL = {
    "name": "record_triage",
    "description": "Record the triage of one scientific paper for a PhD literature database.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "2 to 3 sentences in English: what was studied, where, how, and the main result."},
            "relevance": {"type": "integer", "minimum": 1, "maximum": 5,
                          "description": "Relevance to the PhD, using the rubric in the system prompt."},
            "relevance_reason": {"type": "string",
                                 "description": "One sentence justifying the score with respect to the PhD."},
            "research_questions": {"type": "array", "items": {"type": "string", "enum": RESEARCH_QUESTIONS},
                                   "description": "PhD research questions this paper substantially informs. May be empty."},
            "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}, "maxItems": 3,
                           "description": "One to three subject categories the paper substantially addresses."},
            "ecosystems": {"type": "array", "items": {"type": "string", "enum": ECOSYSTEMS}},
            "gases": {"type": "array", "items": {"type": "string", "enum": GASES},
                      "description": "Greenhouse gases actually measured or modelled. May be empty."},
            "methods": {"type": "string",
                        "description": "Short phrase, e.g. 'static chambers on soil collars and stems, 2 growing seasons'."},
            "key_result": {"type": "string",
                           "description": "The single most important result, one sentence. Do not invent numbers."},
        },
        "required": ["summary", "relevance", "relevance_reason", "research_questions", "categories",
                     "ecosystems", "gases", "methods", "key_result"],
    },
}

TRIAGE_SYSTEM = """You triage scientific papers for a PhD student so they can decide what to read first.

Here is the student's research context:
<context>
{context}
</context>

Relevance rubric:
5 = directly on the PhD topic (GHG fluxes or carbon dynamics in forested swamps or very similar systems), or a key method or framework the student will reuse.
4 = closely related: other wetland types with the same gases or processes, stem fluxes, or microbial drivers of CH4/N2O.
3 = useful background: general wetland carbon cycling, upland forest fluxes, reviews.
2 = tangential.
1 = not relevant.

Rules: base everything only on the title and abstract provided. If the abstract is missing, say so at the start of the summary and keep the relevance score conservative. Never invent numbers, sites or methods that are not in the text. Record your answer with the record_triage tool."""


def _gemini_schema(schema):
    """Convert the JSON schema above into the subset Gemini's responseSchema accepts."""
    out = {"type": schema["type"].upper()}
    if "description" in schema:
        out["description"] = schema["description"]
    if "enum" in schema:
        out["enum"] = schema["enum"]
    if schema["type"] == "object":
        out["properties"] = {k: _gemini_schema(v) for k, v in schema["properties"].items()}
        out["required"] = schema.get("required", [])
    if schema["type"] == "array":
        out["items"] = _gemini_schema(schema["items"])
    return out


class GeminiQuotaError(RuntimeError):
    pass


def ask_gemini(system, user, tool=None):
    """Call Gemini, falling back to the next model when one has no free quota for this key."""
    global GEMINI_MODEL
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": AI_CFG.get("temperature", 0),
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema((tool or TRIAGE_TOOL)["input_schema"]),
        },
    }
    while GEMINI_MODELS:
        GEMINI_MODEL = GEMINI_MODELS[0]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        for attempt in range(4):
            r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=120)
            if r.ok:
                data = r.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                result = json.loads(text)
                result["_model"] = data.get("modelVersion") or GEMINI_MODEL   # the exact version behind an alias
                return result
            detail = r.text[:400].replace("\n", " ")
            if r.status_code == 404 or (r.status_code == 429 and "limit: 0" in r.text):
                log(f"   {GEMINI_MODEL} not usable with this key ({r.status_code}), trying the next model")
                break
            if r.status_code == 429 and ("PerDay" in r.text or "per day" in r.text.lower()):
                raise GeminiQuotaError(f"daily free quota reached on {GEMINI_MODEL}")
            if r.status_code in (429, 500, 503):
                wait = 15 * (attempt + 1)
                log(f"   Gemini {r.status_code} on {GEMINI_MODEL}, waiting {wait}s. Google says: {detail[:200]}")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Gemini {r.status_code} on {GEMINI_MODEL}: {detail}")
        else:
            log(f"   {GEMINI_MODEL} still rate limited after retries, trying the next model")
        GEMINI_MODELS.pop(0)
    raise GeminiQuotaError("no Gemini model has free quota left for this key today")


def ask_claude(client, system, user, tool=None):
    tool = tool or TRIAGE_TOOL
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    return next((b.input for b in msg.content if b.type == "tool_use"), None)


class Triager:
    """Shared AI access for library papers and suggestions. Stops cleanly when the free quota is used up."""

    def __init__(self):
        self.provider = AI_PROVIDER
        self.exhausted = False
        self.client = None
        if self.provider == "none":
            return
        log(f"   using {self.provider} ({', '.join(GEMINI_MODELS) if self.provider == 'gemini' else CLAUDE_MODEL})")
        if self.provider == "claude":
            import anthropic  # only needed if you opt into the paid Claude API
            self.client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        context = (ROOT / "research_context.md").read_text(encoding="utf-8")
        self.system = TRIAGE_SYSTEM.format(context=context)
        self.extract_system = EXTRACT_SYSTEM
        if self.provider == "gemini":
            self.system = self.system.replace("Record your answer with the record_triage tool.",
                                              "Answer in the requested JSON format.")
            self.extract_system = self.extract_system.replace("Record your answer with the record_extraction tool.",
                                                              "Answer in the requested JSON format.")
        self.field_system = FIELD_SYSTEM.format(context=context)
        if self.provider == "gemini":
            self.field_system = self.field_system.replace("Record your answer with the record_field_map tool.",
                                                          "Answer in the requested JSON format.")
        self.calls = 0

    @property
    def model(self):
        if getattr(self, "last_version", None):
            return self.last_version
        return {"gemini": GEMINI_MODEL, "claude": CLAUDE_MODEL}.get(self.provider, "none")

    @property
    def available(self):
        return self.provider != "none" and not self.exhausted

    def run(self, label, title, year, journal, abstract, kind="triage"):
        if not self.available:
            return None
        user = (f"Title: {title}\nYear: {year or 'unknown'}\n"
                f"Journal: {journal or 'unknown'}\n"
                f"Abstract: {abstract or '(no abstract available)'}")
        system, tool = {"triage": (self.system, TRIAGE_TOOL),
                        "extract": (self.extract_system, EXTRACT_TOOL),
                        "field": (getattr(self, "field_system", ""), FIELD_TOOL)}[kind]
        phash = prompt_hash(system, tool)
        _PROMPTS_SEEN[phash] = {"kind": kind, "system": system, "schema": tool}
        try:
            self.calls += 1
            if self.provider == "gemini":
                result = ask_gemini(system, user, tool)
                time.sleep(GEMINI_SECONDS_BETWEEN_CALLS)
            else:
                result = ask_claude(self.client, system, user, tool)
        except GeminiQuotaError as e:
            log(f"   AI quota reached ({e}), the rest waits for the next run")
            self.exhausted = True
            return None
        except Exception as e:
            log(f"   AI error on {label}: {e}")
            return None
        if not result:
            log(f"   no answer returned for {label}")
            return None
        version = result.pop("_model", None) or self.model
        self.last_version = version
        log_ai_call(kind, label, version, phash, result)
        result["_model"], result["_prompt"] = version, phash
        if kind == "triage":
            try:
                result["relevance"] = max(1, min(5, int(result.get("relevance", 1))))
            except (TypeError, ValueError):
                result["relevance"] = 1
        return result


def keep(values, allowed):
    return [v for v in (values or []) if v in allowed]


def triage_papers(papers, triager):
    if triager.provider == "none":
        log("   skipped (no GEMINI_API_KEY, or --no-ai)")
        return 0
    todo = [p for p in papers if not p["triage_date"]]
    todo.sort(key=lambda p: p["status"] == "Read")  # unread papers first
    done = 0
    for p in todo[:MAX_TRIAGE_PER_RUN]:
        result = triager.run(p["name"], p["title"], p["year"], p["journal"], p["abstract"])
        if triager.exhausted:
            break
        if not result:
            continue
        props = {
            "Summary": p_text(result.get("summary")),
            "Relevance": p_num(result["relevance"]),
            "Relevance reason": p_text(result.get("relevance_reason")),
            "Research questions": p_multi(keep(result.get("research_questions"), RESEARCH_QUESTIONS)),
            "Ecosystem": p_multi(keep(result.get("ecosystems"), ECOSYSTEMS)),
            "Gases": p_multi(keep(result.get("gases"), GASES)),
            "Methods": p_text(result.get("methods")),
            "Key result": p_text(result.get("key_result")),
            "Triage date": p_date(TODAY),
        }
        if not p["category"]:  # never overwrite a category you set yourself
            cats = keep(result.get("categories"), CATEGORIES)[:3]
            props["Category"] = p_multi(cats)
            p["category"] = cats
        update_page(p["page_id"], props)
        p["relevance"] = result["relevance"]
        p["summary"] = result.get("summary") or ""
        p["key_result"] = result.get("key_result") or ""
        p["rqs"] = keep(result.get("research_questions"), RESEARCH_QUESTIONS)
        p["ecosystems"] = keep(result.get("ecosystems"), ECOSYSTEMS)
        p["gases"] = keep(result.get("gases"), GASES)
        done += 1
    left = max(0, len(todo) - done)
    log(f"   triaged {done} papers" + (f", {left} left for the next runs" if left else ""))
    return done


# ----------------------------------------------------------------------------
# Step 2b: structured extraction (study sites, design, reported values)
# ----------------------------------------------------------------------------
EXTRACT_TOOL = {
    "name": "record_extraction",
    "description": "Record structured information extracted from one scientific abstract.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sites": {
                "type": "array",
                "description": "Field sites where data were collected. Empty for reviews, models without sites, or when no place is named.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Site name as written, e.g. 'Mer Bleue bog'."},
                        "region": {"type": "string", "description": "Province, state or region, if stated or obvious from the site."},
                        "country": {"type": "string", "description": "Country, if stated or obvious from the site."},
                    },
                    "required": ["name", "region", "country"],
                },
            },
            "biome": {"type": "string", "enum": BIOMES},
            "study_design": {"type": "string", "enum": STUDY_DESIGNS},
            "duration": {"type": "string", "description": "Study period or duration, e.g. '2 growing seasons (2019-2020)'. Empty if not stated."},
            "reported_values": {
                "type": "array",
                "description": "Quantitative results exactly as stated in the abstract. Never compute or invent values.",
                "items": {
                    "type": "object",
                    "properties": {
                        "gas": {"type": "string", "description": "CO2, CH4, N2O or other quantity."},
                        "value": {"type": "string", "description": "Value or range as written, with sign."},
                        "unit": {"type": "string"},
                        "context": {"type": "string", "description": "What the value refers to, a few words."},
                    },
                    "required": ["gas", "value", "unit", "context"],
                },
            },
        },
        "required": ["sites", "biome", "study_design", "duration", "reported_values"],
    },
}

EXTRACT_SYSTEM = """You extract structured data from scientific abstracts for a literature database.
Only use information present in the title and abstract. Never invent sites, numbers or units.
For sites, give the most specific place named; add region and country when they are stated or unambiguous.
Use biome "Not stated" when the abstract gives no clue. Record your answer with the record_extraction tool."""


class GeocodeBudgetSpent(Exception):
    pass


_geo_budget = {"left": None}   # None = unlimited (library); a number during the field map step


def geocode(query, state):
    """Free geocoding with OpenStreetMap Nominatim (max 1 request per second), cached between runs."""
    cache = state.setdefault("geocode", {})
    if query in cache and (cache[query] is None or len(cache[query]) >= 4):
        return cache[query]          # entries from older versions (no country/province) are refreshed
    if _geo_budget["left"] is not None:
        if _geo_budget["left"] <= 0:
            raise GeocodeBudgetSpent()
        _geo_budget["left"] -= 1
    time.sleep(1.1)
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": query, "format": "json", "limit": 1, "addressdetails": 1,
                                 "accept-language": "en"},
                         headers={"User-Agent": f"lit-pipeline/1.0 ({CONTACT_EMAIL or 'personal research tool'})"},
                         timeout=30)
        hits = r.json() if r.ok else []
    except Exception:
        hits = []
    result = None
    if hits:
        addr = hits[0].get("address") or {}
        result = [float(hits[0]["lat"]), float(hits[0]["lon"]), (addr.get("country_code") or "").lower(),
                  addr.get("state") or addr.get("province") or addr.get("region") or ""]
    cache[query] = result
    return result


def locate_sites(sites, state):
    """Geocode each site, falling back to region then country. Returns points with their precision."""
    points = []
    for site in sites or []:
        name, region, country = (site.get(k, "").strip() for k in ("name", "region", "country"))
        attempts = [(", ".join(x for x in (name, region, country) if x) if name else "", "site"),
                    (", ".join(x for x in (region, country) if x), "region"),
                    (country, "country")]
        for query, precision in attempts:
            if not query:
                continue
            coords = geocode(query, state)
            if coords:
                points.append({"label": ", ".join(x for x in (name, region, country) if x),
                               "lat": coords[0], "lon": coords[1], "precision": precision,
                               "cc": coords[2], "state": coords[3] if precision != "country" else ""})
                break
    return points


def extract_papers(papers, triager, state):
    sites_store = state.setdefault("sites", {})
    if not triager.available:
        return 0
    todo = [p for p in papers if not p.get("extraction_date")]
    done = 0
    for p in todo[:MAX_EXTRACT_PER_RUN]:
        result = triager.run(p["name"], p["title"], p["year"], p["journal"], p["abstract"], kind="extract")
        if triager.exhausted:
            break
        if not result:
            continue
        sites = result.get("sites") or []
        points = locate_sites(sites, state)
        sites_store[p["page_id"]] = points
        values = [f"{v.get('gas', '')}: {v.get('value', '')} {v.get('unit', '')} ({v.get('context', '')})".strip()
                  for v in (result.get("reported_values") or [])]
        biome = result.get("biome") if result.get("biome") in BIOMES else "Not stated"
        design = result.get("study_design") if result.get("study_design") in STUDY_DESIGNS else None
        sites_text = "; ".join(", ".join(x for x in (s.get("name"), s.get("region"), s.get("country")) if x)
                               for s in sites)
        update_page(p["page_id"], {
            "Study sites": p_text(sites_text),
            "Biome": p_select(biome),
            "Study design": p_select(design),
            "Study duration": p_text(result.get("duration")),
            "Reported values": p_text("\n".join(values)),
            "Extraction date": p_date(TODAY),
        })
        p.update({"biome": biome, "design": design, "sites_text": sites_text, "extraction_date": TODAY})
        done += 1
    left = max(0, len(todo) - done)
    log(f"   extracted {done} papers" + (f", {left} left for the next runs" if left else ""))
    return done


# ----------------------------------------------------------------------------
# Persistent state (committed to the repo each night: the audit trail)
# ----------------------------------------------------------------------------
def load_state():
    path = DATA_DIR / "state.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("   warning: data/state.json unreadable, starting a fresh state")
    return {}


def save_state(state):
    if DRY_RUN:
        return
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True),
                                         encoding="utf-8")


def append_csv(name, header, rows):
    if DRY_RUN or not rows:
        return
    import csv
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / name
    if path.exists():
        with path.open(encoding="utf-8") as f:
            first = f.readline().strip()
        if first != ",".join(header):   # columns changed: archive the old file instead of misaligning rows
            path.rename(path.with_name(path.stem + f"_until_{TODAY}" + path.suffix))
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


# ----------------------------------------------------------------------------
# Step 3: suggestions (Connected Papers-style similarity + your own relevance)
# ----------------------------------------------------------------------------
def recency(year):
    if not year:
        return 0.5
    return max(0.0, 1 - (dt.date.today().year - year) / 15)


def global_score(relevance, similarity, connections, pct, year, semantic=None):
    """Same formula as the 'Global score' property in Notion. If you change one, change the other."""
    return round(100 * (
        0.30 * (relevance / 5 if relevance else 0.5)
        + 0.25 * (similarity or 0) / 100
        + 0.15 * (semantic / 100 if semantic is not None else 0.5)
        + 0.12 * min(connections or 0, 5) / 5
        + 0.10 * (pct / 100 if pct is not None else 0.5)
        + 0.08 * recency(year)
    ))


def semantic_scholar_recommendations(dois):
    if not dois:
        return []
    headers = {"x-api-key": S2_KEY} if S2_KEY else {}
    body = {"positivePaperIds": [f"DOI:{d}" for d in dois[:100]]}
    for attempt in range(4):
        r = requests.post("https://api.semanticscholar.org/recommendations/v1/papers",
                          params={"limit": 60, "fields": "title,externalIds"},
                          json=body, headers=headers, timeout=60)
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        if not r.ok:
            log(f"   Semantic Scholar -> {r.status_code}, skipping recommendations")
            return []
        recs = r.json().get("recommendedPapers", [])
        return [norm_doi((x.get("externalIds") or {}).get("DOI")) for x in recs
                if (x.get("externalIds") or {}).get("DOI")]
    return []


def fetch_citers(lib_ids):
    """Reference lists of papers that cite your library (used for co-citation and derivative works)."""
    citers = {}
    for chunk in chunks(sorted(lib_ids), 40):
        for page in range(1, CITER_PAGES + 1):
            res = openalex("/works", {"filter": "cites:" + "|".join(chunk), "per_page": 200, "page": page,
                                      "sort": "cited_by_count:desc", "select": "id,referenced_works"})
            results = (res or {}).get("results", [])
            for c in results:
                citers[short_id(c["id"])] = {short_id(r) for r in (c.get("referenced_works") or [])}
            if len(results) < 200:
                break
    return citers


def load_existing_suggestions():
    existing = {}
    for page in query_all(SUGGEST_DS):
        pr = page["properties"]
        oid = short_id(read(pr.get("OpenAlex ID")))
        if oid:
            existing[oid] = {
                "page_id": page["id"],
                "title": read(pr.get("Title")) or "",
                "decision": read(pr.get("Decision")),
                "reason": read(pr.get("Exclusion reason")),
                "relevance": read(pr.get("Relevance")),
                "similarity": read(pr.get("Similarity")),
                "connections": read(pr.get("Connection score")),
                "percentile": read(pr.get("Citation percentile")),
                "year": read(pr.get("Year")),
                "triage_date": read(pr.get("Triage date")),
                "first_suggested": read(pr.get("First suggested")),
                "personal": read(pr.get("Personal score")),
                "roles": read(pr.get("Role")) or [],
                "summary": read(pr.get("Summary")) or "",
                "abstract": read(pr.get("Abstract")) or "",
                "semantic": read(pr.get("Semantic match")),
            }
    return existing


def build_suggestions(papers, existing, keep_n=None, emb=None, sem_extra=None):
    lib = [p for p in papers if p.get("work")]
    lib_ids = {short_id(p["work"]["id"]) for p in lib}
    lib_dois = {p["doi"] for p in papers if p["doi"]}
    name_of = {short_id(p["work"]["id"]): p["name"] for p in lib}
    lib_refs = {short_id(p["work"]["id"]): {short_id(r) for r in (p["work"].get("referenced_works") or [])}
                for p in lib}
    # Similarity to a paper you rated 5/5 counts more than to one you rated 2/5.
    weight = {short_id(p["work"]["id"]): (p.get("relevance") or 3) / 5 for p in lib}

    cited_by_lib = defaultdict(set)                    # X -> your papers that cite X   (prior work)
    cites_lib = defaultdict(set)                       # X -> your papers that X cites  (derivative work)
    cocite = defaultdict(lambda: defaultdict(int))     # X -> your paper -> times cited together
    appear = defaultdict(int)                          # X -> sampled citing papers that also cite X
    lib_appear = defaultdict(int)                      # your paper -> sampled citing papers

    for lid, refs in lib_refs.items():
        for r in refs - lib_ids:
            cited_by_lib[r].add(lid)

    citers = fetch_citers(lib_ids)
    for cid, refs in citers.items():
        hits = refs & lib_ids
        if not hits:
            continue
        if cid not in lib_ids:
            cites_lib[cid] |= hits
        for lid in hits:
            lib_appear[lid] += 1
        for r in refs - lib_ids:
            appear[r] += 1
            for lid in hits:
                cocite[r][lid] += 1
    log(f"   {len(citers)} citing papers sampled for co-citation")

    s2 = set()
    rec_dois = [d for d in semantic_scholar_recommendations(sorted(lib_dois)) if d and d not in lib_dois]
    for chunk in chunks(rec_dois, 50):
        for w in works_filter("doi:" + "|".join(chunk), per_page=50, select="id"):
            s2.add(short_id(w["id"]))

    # Cheap pre-ranking, then score the best CANDIDATE_POOL in depth.
    sem_extra = {k: v for k, v in (sem_extra or {}).items() if k not in lib_ids}
    candidates = (set(cited_by_lib) | set(cites_lib) | set(cocite) | s2 | set(sem_extra)) - lib_ids
    pre = {x: 2 * len(cited_by_lib[x]) + 2 * len(cites_lib[x]) + sum(cocite[x].values()) + (2 if x in s2 else 0)
              + (3 if x in sem_extra else 0)
           for x in candidates}
    pool = [x for x, v in sorted(pre.items(), key=lambda kv: kv[1], reverse=True) if v >= 2][:CANDIDATE_POOL]
    pool += [x for x in sem_extra if x not in pool]      # papers found by meaning are always scored in depth

    meta = {}
    for chunk in chunks(pool, 50):
        for w in works_filter("openalex_id:" + "|".join(chunk), per_page=50):
            meta[short_id(w["id"])] = w

    lib_ref_union = set().union(*lib_refs.values()) if lib_refs else set()
    scored = []
    for x in pool:
        w = meta.get(x)
        if not w or w.get("type") in SKIP_WORK_TYPES or norm_doi(w.get("doi")) in lib_dois:
            continue
        refs_x = {short_id(r) for r in (w.get("referenced_works") or [])}
        raw, per_lib = 0.0, {}
        for lid, refs_l in lib_refs.items():
            coupling = (len(refs_x & refs_l) / math.sqrt(len(refs_x) * len(refs_l))) if refs_x and refs_l else 0.0
            co = cocite[x].get(lid, 0)
            cocitation = co / math.sqrt(max(1, appear[x]) * max(1, lib_appear[lid])) if co else 0.0
            direct = 1.0 if (lid in cited_by_lib[x] or lid in cites_lib[x]) else 0.0
            sim = 0.45 * coupling + 0.45 * cocitation + 0.10 * direct
            per_lib[lid] = sim
            raw += weight[lid] * sim
        scored.append({"id": x, "work": w, "raw": raw, "per_lib": per_lib, "refs": refs_x})

    top_raw = max((c["raw"] for c in scored), default=0) or 1
    if emb is not None and scored:      # meaning-based match with your research questions
        import semantic
        _, sem = semantic.match_scores(emb, CONFIG, RESEARCH_QUESTIONS,
                                       [semantic.paper_text(c["work"].get("display_name"),
                                                            abstract_text(c["work"].get("abstract_inverted_index")))
                                        for c in scored])
        for c, v in zip(scored, sem):
            c["semantic"] = v
    for c in scored:
        x, w = c["id"], c["work"]
        c["similarity"] = round(100 * c["raw"] / top_raw)
        c["cited_by"] = {name_of[l] for l in cited_by_lib[x]}
        c["cites"] = {name_of[l] for l in cites_lib[x]}
        c["s2"] = x in s2
        c["score"] = len(cited_by_lib[x] | cites_lib[x]) + (1 if c["s2"] else 0)
        c["shared_refs"] = len(c["refs"] & lib_ref_union)
        c["cocitations"] = appear[x]
        roles = []
        if len(cited_by_lib[x]) >= 2:
            roles.append("Prior work")
        if len(cites_lib[x]) >= 2:
            roles.append("Derivative work")
        if c["similarity"] >= 40 or not roles:
            roles.append("Similar work")
        c["roles"] = roles
        # the two of your papers it is most similar to (used for the map and the explanation)
        c["closest"] = [l for l, v in sorted(c["per_lib"].items(), key=lambda kv: kv[1], reverse=True)[:2] if v > 0]

        reasons, sources = [], []
        if c["cited_by"]:
            reasons.append(f"Cited by {len(c['cited_by'])} of your papers ({'; '.join(sorted(c['cited_by'])[:4])})")
            sources.append("Cited by your papers")
        if c["cites"]:
            reasons.append(f"Cites {len(c['cites'])} of your papers ({'; '.join(sorted(c['cites'])[:4])})")
            sources.append("Cites your papers")
        if c["s2"]:
            reasons.append("Recommended by Semantic Scholar from your library")
            sources.append("Semantic Scholar recommendation")
        c.setdefault("semantic", None)
        if x in sem_extra:
            reasons.append(f"Found by meaning: close to your research questions (semantic match {sem_extra[x]})")
            sources.append("Semantic search")
        if c["closest"]:
            reasons.append("Most similar to " + " and ".join(name_of[l] for l in c["closest"]))
        if c["shared_refs"]:
            reasons.append(f"Shares {c['shared_refs']} reference{'s' if c['shared_refs'] > 1 else ''} with your library")
        c["why"] = ". ".join(reasons) + "."
        c["sources"] = sources
        prev = existing.get(x, {})
        c["relevance"] = prev.get("relevance")
        c["summary"] = prev.get("summary") or ""
        c["global"] = global_score(c["relevance"], c["similarity"], c["score"],
                                   percentile(w), w.get("publication_year"), c["semantic"])

    scored.sort(key=lambda c: c["global"], reverse=True)
    top = scored[:keep_n or MAX_SUGGESTIONS]
    # Papers found by meaning have few citation links, so they rank low on similarity: reserve them some slots.
    reserved = int((CONFIG.get("semantic") or {}).get("reserved_slots", 8)) if keep_n is None else 0
    sem_found = [c for c in scored if c["id"] in sem_extra and c not in top][:max(0, reserved - sum(c["id"] in sem_extra for c in top))]
    if sem_found:
        top = top[:len(top) - len(sem_found)] + sem_found
    top_ids = {c["id"] for c in top}
    # also refresh suggestions already in Notion that are still in the scored pool
    refresh = [c for c in scored if c["id"] in existing and c["id"] not in top_ids]
    log(f"   {len(candidates)} connected papers found, {len(scored)} scored in depth, "
        f"keeping the top {len(top)}")
    return top, refresh, lib_ids, candidates


# ---- Learning from your decisions -------------------------------------------
FEATURES = ["AI relevance", "Similarity", "Direct citation links", "Citation impact", "Recency"]


def feature_vector(relevance, similarity, connections, pct, year):
    return [relevance / 5 if relevance else 0.5,
            (similarity or 0) / 100,
            min(connections or 0, 5) / 5,
            pct / 100 if pct is not None else 0.5,
            recency(year)]


def _sigmoid(z):
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def train_preferences(existing):
    """Logistic regression on your Added / Not relevant decisions (pure Python, no dependencies)."""
    X, y = [], []
    for info in existing.values():
        if info["decision"] in ("Added to Zotero", "Not relevant"):
            X.append(feature_vector(info["relevance"], info["similarity"], info["connections"],
                                    info["percentile"], info["year"]))
            y.append(1 if info["decision"] == "Added to Zotero" else 0)
    n_pos, n_neg = sum(y), len(y) - sum(y)
    if len(y) < MIN_DECISIONS_TO_LEARN or n_pos < 3 or n_neg < 3:
        return {"ready": False, "n": len(y), "added": n_pos, "excluded": n_neg,
                "needed": MIN_DECISIONS_TO_LEARN}
    w = _fit_logistic(X, y)
    scores = [sum(a * b for a, b in zip(w, xi + [1])) for xi in X]
    auc_in = auc_score(scores, y)
    # 5-fold cross-validated AUC: accuracy on decisions the model did not learn from
    folds = 5 if len(y) >= 25 else len(y)            # leave-one-out for small sets
    order = sorted(range(len(y)), key=lambda i: hashlib.md5(str(i).encode()).hexdigest())
    cv_scores, cv_y = [], []
    for f in range(folds):
        test = [order[i] for i in range(len(order)) if i % folds == f]
        train = [i for i in range(len(y)) if i not in test]
        if len({y[i] for i in train}) < 2:
            continue
        wf = _fit_logistic([X[i] for i in train], [y[i] for i in train], iters=1500)
        cv_scores += [sum(a * b for a, b in zip(wf, X[i] + [1])) for i in test]
        cv_y += [y[i] for i in test]
    return {"ready": True, "n": len(y), "added": n_pos, "excluded": n_neg, "weights": w[:-1],
            "intercept": w[-1], "auc": round(auc_in, 2), "auc_cv": round(auc_score(cv_scores, cv_y), 2)}


def _fit_logistic(X, y, iters=4000, lr=0.5, l2=0.05):
    w = [0.0] * (len(X[0]) + 1)          # last weight is the intercept
    for _ in range(iters):
        grad = [0.0] * len(w)
        for xi, yi in zip(X, y):
            err = _sigmoid(sum(a * b for a, b in zip(w, xi + [1]))) - yi
            for j, v in enumerate(xi + [1]):
                grad[j] += err * v
        for j in range(len(w)):
            reg = l2 * w[j] if j < len(w) - 1 else 0
            w[j] -= lr * (grad[j] / len(y) + reg)
    return w


def auc_score(scores, labels):
    pos = [s for s, t in zip(scores, labels) if t]
    neg = [s for s, t in zip(scores, labels) if not t]
    if not pos or not neg:
        return float("nan")
    return sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg))


def personal_score(model, relevance, similarity, connections, pct, year):
    if not model.get("ready"):
        return None
    x = feature_vector(relevance, similarity, connections, pct, year)
    return round(100 * _sigmoid(sum(a * b for a, b in zip(model["weights"], x)) + model["intercept"]))


PREDICTIONS = {}   # bound to state["predictions"] in main()
DUPLICATES = []    # papers present twice in the library (reported on the dashboard)


def prospective_eval(existing, state):
    """AUC of personal scores recorded BEFORE you decided: the honest, forward-looking test."""
    preds, dates = state.get("predictions", {}), state.get("decision_dates", {})
    scores, labels = [], []
    for oid, (pdate, score) in preds.items():
        info = existing.get(oid)
        if info and info["decision"] in ("Added to Zotero", "Not relevant") and dates.get(oid, "") > pdate:
            scores.append(score)
            labels.append(1 if info["decision"] == "Added to Zotero" else 0)
    auc = auc_score(scores, labels)
    return {"n": len(labels), "auc": None if auc != auc else round(auc, 2)}


def track_decisions(existing, state):
    """Compare decisions with last run's snapshot and log every change (the screening audit trail)."""
    snap = state.setdefault("decisions", {})
    events = []
    for oid, info in existing.items():
        current = [info["decision"] or "", info["reason"] or ""]
        if info["decision"] != "To review":
            state.setdefault("decision_dates", {}).setdefault(oid, TODAY)
        if snap.get(oid) != current:
            if snap.get(oid) is not None or info["decision"] != "To review":
                events.append([TODAY, oid, info["title"][:200], (snap.get(oid) or ["", ""])[0],
                               current[0], current[1]])
            snap[oid] = current
    append_csv("decisions.csv", ["date", "openalex_id", "title", "previous_decision", "decision",
                                 "exclusion_reason"], events)
    return len(events)


def sync_suggestions(top, refresh, lib_ids, existing, triager, scorer, emb=None):
    # Suggestions you have since added to Zotero get marked automatically.
    for oid, info in existing.items():
        if oid in lib_ids and info["decision"] == "To review":
            update_page(info["page_id"], {"Decision": p_select("Added to Zotero")})

    created = updated = 0
    to_triage = []
    for c in top + refresh:
        w = c["work"]
        props = {
            "Connection score": p_num(c["score"]),
            "Similarity": p_num(c["similarity"]),
            "Role": p_multi(c["roles"]),
            "Shared references": p_num(c["shared_refs"]),
            "Co-citations": p_num(c["cocitations"]),
            "Why suggested": p_text(c["why"]),
            "Source": p_multi(c["sources"]),
            "Citations": p_num(w.get("cited_by_count")),
            "Citation percentile": p_num(percentile(w)),
        }
        if c.get("semantic") is not None:
            props["Semantic match"] = p_num(c["semantic"])
        ps = scorer(c["work"].get("display_name"), abstract_text(w.get("abstract_inverted_index")), c["relevance"],
                    c["similarity"], c["score"], percentile(w), w.get("publication_year"))
        if ps is not None:
            props["Personal score"] = p_num(ps)
            PREDICTIONS.setdefault(c["id"], [TODAY, ps])
        c["personal"] = ps
        info = existing.get(c["id"])
        if info:
            update_page(info["page_id"], props)
            updated += 1
            if not info["triage_date"]:
                to_triage.append((info["page_id"], c))
            continue
        props.update({
            "Title": p_title(w.get("display_name")),
            "Authors": p_text(author_list(w)),
            "Year": p_num(w.get("publication_year")),
            "Journal": p_text(journal(w)),
            "DOI": p_url(w.get("doi")),
            "OpenAlex ID": p_url(w["id"]),
            "Abstract": p_text(abstract_text(w.get("abstract_inverted_index"))),
            "Decision": p_select("To review"),
            "First suggested": p_date(TODAY),
        })
        page_id = create_page(SUGGEST_DS, props)
        created += 1
        if page_id:
            to_triage.append((page_id, c))
    log(f"   {created} new suggestions, {updated} updated")
    new_ids = {c["id"] for c in top + refresh if c["id"] not in existing}

    # Older pending suggestions not re-scored this run: refresh Personal score and Semantic match
    scored_ids = {c["id"] for c in top + refresh}
    older = [(oid, info) for oid, info in existing.items() if oid not in scored_ids and info["decision"] == "To review"]
    sems = [None] * len(older)
    if emb is not None and older:
        import semantic
        _, sems = semantic.match_scores(emb, CONFIG, RESEARCH_QUESTIONS,
                                        [semantic.paper_text(i["title"], i["abstract"]) for _, i in older])
    for (oid, info), sem in zip(older, sems):
        props = {}
        ps = scorer(info["title"], info["abstract"], info["relevance"], info["similarity"], info["connections"],
                    info["percentile"], info["year"])
        if ps is not None and ps != info["personal"]:
            props["Personal score"] = p_num(ps)
        if sem is not None and sem != info["semantic"]:
            props["Semantic match"] = p_num(sem)
        if props:
            update_page(info["page_id"], props)

    stats = {"created": created, "new_ids": new_ids, "new_relevant": 0, "triaged": 0}
    # AI relevance for suggestions, best candidates first, within the free quota left today
    if not triager.available:
        if triager.provider != "none":
            log("   suggestion triage postponed (AI quota used up today)")
        return stats
    to_triage.sort(key=lambda pc: pc[1]["global"], reverse=True)
    done = 0
    for page_id, c in to_triage[:MAX_SUGGESTION_TRIAGE_PER_RUN]:
        w = c["work"]
        result = triager.run(w.get("display_name"), w.get("display_name"), w.get("publication_year"),
                             journal(w), abstract_text(w.get("abstract_inverted_index")))
        if triager.exhausted:
            break
        if not result:
            continue
        update_page(page_id, {
            "Summary": p_text(result.get("summary")),
            "Relevance": p_num(result["relevance"]),
            "Relevance reason": p_text(result.get("relevance_reason")),
            "Research questions": p_multi(keep(result.get("research_questions"), RESEARCH_QUESTIONS)),
            "Triage date": p_date(TODAY),
        })
        c["relevance"] = result["relevance"]
        c["summary"] = result.get("summary") or ""
        c["global"] = global_score(c["relevance"], c["similarity"], c["score"],
                                   percentile(w), w.get("publication_year"), c.get("semantic"))
        if c["id"] in existing:
            existing[c["id"]]["relevance"] = c["relevance"]
        done += 1
    left = max(0, len(to_triage) - done)
    log(f"   AI-scored {done} suggestions" + (f", {left} left for the next runs" if left else ""))
    stats["triaged"] = done
    stats["new_relevant"] = sum(1 for c in top + refresh if c["id"] in new_ids and (c["relevance"] or 0) >= 4)
    return stats


# ----------------------------------------------------------------------------
# Step 4: graph
# ----------------------------------------------------------------------------
def build_graph(papers, suggestions):
    lib = [p for p in papers if p.get("work")]
    lib_ids = {short_id(p["work"]["id"]) for p in lib}
    lib_by_name = {p["name"]: short_id(p["work"]["id"]) for p in lib}
    nodes, edges, seen = [], [], set()

    def add_edge(a, b, kind, strength=None):
        key = (a, b, kind) if kind == "cites" else (tuple(sorted((a, b))), kind)
        if key not in seen:
            seen.add(key)
            e = {"from": a, "to": b, "kind": kind}
            if strength is not None:
                e["strength"] = strength
            edges.append(e)

    seen_ids = set()
    for p in lib:
        w = p["work"]
        if short_id(w["id"]) in seen_ids:      # the same paper twice in the library: one node only
            continue
        seen_ids.add(short_id(w["id"]))
        nodes.append({
            "id": short_id(w["id"]),
            "label": p["name"],
            "title": p["title"],
            "year": p["year"] or w.get("publication_year"),
            "citations": w.get("cited_by_count") or 0,
            "percentile": percentile(w),
            "category": (p["category"] or ["Uncategorized"])[0],
            "read": p["status"] == "Read",
            "doi": w.get("doi") or (f"https://doi.org/{p['doi']}" if p["doi"] else None),
            "notion": p.get("notion_url"),
            "kind": "library",
        })
        for ref in w.get("referenced_works") or []:
            rid = short_id(ref)
            if rid in lib_ids:
                add_edge(short_id(w["id"]), rid, "cites")

    # Similarity links between your own papers (shared references), Connected Papers-style:
    # each paper keeps its 3 strongest links with at least 2 shared references.
    refs = {short_id(p["work"]["id"]): {short_id(r) for r in (p["work"].get("referenced_works") or [])} for p in lib}
    ids = [i for i in refs if i in seen_ids]
    for a in ids:
        links = []
        for b in ids:
            if a == b or not refs[a] or not refs[b]:
                continue
            shared = len(refs[a] & refs[b])
            if shared >= 2:
                links.append((shared / math.sqrt(len(refs[a]) * len(refs[b])), b))
        for strength, b in sorted(links, reverse=True)[:3]:
            add_edge(a, b, "libsim", round(strength, 3))

    shown = sorted(suggestions, key=lambda c: c["global"], reverse=True)[:GRAPH_SUGGESTIONS]
    for c in shown:
        if c["id"] in seen_ids:
            continue
        seen_ids.add(c["id"])
        w = c["work"]
        nodes.append({
            "id": c["id"],
            "label": f"{first_author(w)} et al., {w.get('publication_year') or 'n.d.'}",
            "title": w.get("display_name"),
            "year": w.get("publication_year"),
            "citations": w.get("cited_by_count") or 0,
            "percentile": percentile(w),
            "category": "Suggested",
            "read": False,
            "doi": w.get("doi"),
            "notion": None,
            "kind": "suggested",
            "why": c["why"],
            "roles": c["roles"],
            "similarity": c["similarity"],
            "relevance": c["relevance"],
            "semantic": c.get("semantic"),
            "global": c["global"],
        })
        for name in c["cited_by"]:
            add_edge(lib_by_name[name], c["id"], "cites")
        for name in c["cites"]:
            add_edge(c["id"], lib_by_name[name], "cites")
        for lid in c["closest"]:
            add_edge(c["id"], lid, "similar")

    return {"nodes": nodes, "edges": edges}


# ----------------------------------------------------------------------------
# Step 6: field-wide systematic map (nested scopes, see scopes.json)
# ----------------------------------------------------------------------------
FIELD_TOOL = {
    "name": "record_field_map",
    "description": "Classify one paper for a systematic map of the research field.",
    "input_schema": {
        "type": "object",
        "properties": {
            "on_topic": {"type": "boolean",
                         "description": "True if the paper actually studies greenhouse gas or carbon exchange in wetland ecosystems (field, lab, model or review). False for papers that only mention these words."},
            "research_questions": {"type": "array", "items": {"type": "string", "enum": RESEARCH_QUESTIONS},
                                   "description": "Research questions of the PhD that this paper substantially informs. May be empty."},
            "ecosystems": {"type": "array", "items": {"type": "string", "enum": ECOSYSTEMS}},
            "gases": {"type": "array", "items": {"type": "string", "enum": GASES}},
            "biome": {"type": "string", "enum": BIOMES},
            "study_design": {"type": "string", "enum": STUDY_DESIGNS},
            "sites": {"type": "array", "description": "Named field sites. Empty if none are named.",
                      "items": {"type": "object",
                                "properties": {"name": {"type": "string"}, "region": {"type": "string"},
                                               "country": {"type": "string"}},
                                "required": ["name", "region", "country"]}},
        },
        "required": ["on_topic", "research_questions", "ecosystems", "gases", "biome", "study_design", "sites"],
    },
}

FIELD_SYSTEM = """You classify papers for a systematic map of a research field, so a PhD student can see what the field as a whole has and has not studied.

The student's research context, including the research questions to tag:
<context>
{context}
</context>

The paper may be written in French or another language: classify it all the same, using the English labels provided.
Classify the paper from its title and abstract only. Tag a research question only if the paper substantially informs it. Never invent sites. Use biome "Not stated" when there is no clue. Record your answer with the record_field_map tool."""

FIELD_SELECT = "id,doi,display_name,publication_year,type,cited_by_count"


def fetch_abstracts(work_ids):
    """Abstracts are fetched just before classification and never stored (many are publisher-copyrighted)."""
    out = {}
    for chunk in chunks(work_ids, 50):
        for w in works_filter("openalex_id:" + "|".join(chunk), per_page=50, select="id,abstract_inverted_index"):
            out[short_id(w["id"])] = abstract_text(w.get("abstract_inverted_index"))[:4000]
    return out


def load_scopes():
    path = ROOT / "scopes.json"
    if not path.exists():
        return None
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("max_full", 4000)
    cfg.setdefault("sample_size", 1500)
    cfg.setdefault("max_classify_per_run", 200)
    cfg.setdefault("max_new_geocodes_per_run", 120)
    return cfg


def load_corpus():
    path = DATA_DIR / "corpus.json"
    if path.exists():
        try:
            corpus = json.loads(path.read_text(encoding="utf-8"))
            for rec in corpus.values():
                rec.pop("a", None)        # earlier versions stored abstracts: removed
            return corpus
        except json.JSONDecodeError:
            log("   warning: data/corpus.json unreadable, rebuilding the field corpus")
    return {}


def save_corpus(corpus):
    if DRY_RUN:
        return
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "corpus.json").write_text(json.dumps(corpus, ensure_ascii=False, separators=(",", ":")),
                                          encoding="utf-8")


def _scope_filter(query, types="article|review"):
    return f"title_and_abstract.search:{query},type:{types}"


def fetch_scope(scope, cfg, corpus, state):
    """Download the papers of one scope: all of them if it is small enough, otherwise a fixed random sample."""
    meta = state.setdefault("field_scopes", {}).setdefault(scope["id"], {})
    flt = _scope_filter(scope["query"], cfg.get("types", "article|review"))
    res = openalex("/works", {"filter": flt, "per_page": 1, "select": "id"})
    if not res:
        log(f"   {scope['label']}: OpenAlex did not answer, keeping the previous corpus")
        return
    total = res.get("meta", {}).get("count", 0)
    mode = "full" if total <= cfg["max_full"] else "sample"
    key = hashlib.md5(f"{scope['query']}|{mode}|{cfg['sample_size']}|{cfg.get('types', '')}".encode()).hexdigest()
    last = meta.get("last_fetch")
    age = (dt.date.today() - dt.date.fromisoformat(last)).days if last else 9999
    stale = meta.get("key") != key or (age >= 7 if mode == "full" else age >= 120)
    meta.update({"total": total, "mode": mode, "label": scope["label"]})
    if not stale:
        return
    ids = set()

    def add(w):
        wid = short_id(w["id"])
        ids.add(wid)
        rec = corpus.setdefault(wid, {"t": w.get("display_name") or "", "y": w.get("publication_year"),
                                      "d": w.get("doi"), "s": []})
        rec["c"] = w.get("cited_by_count") or 0
        if scope["id"] not in rec["s"]:
            rec["s"].append(scope["id"])

    if mode == "full":
        cursor = "*"
        while cursor:
            page = openalex("/works", {"filter": flt, "per_page": 200, "cursor": cursor, "select": FIELD_SELECT})
            if not page:
                break
            for w in page.get("results", []):
                add(w)
            cursor = page.get("meta", {}).get("next_cursor")
            if not page.get("results"):
                break
    else:
        size = min(cfg["sample_size"], 10000)
        for pg in range(1, math.ceil(size / 200) + 1):
            page = openalex("/works", {"filter": flt, "sample": size, "seed": 42, "per_page": 200,
                                       "page": pg, "select": FIELD_SELECT})
            for w in (page or {}).get("results", []):
                add(w)
    # papers that left this scope (query edited) lose its membership
    for wid, rec in corpus.items():
        if scope["id"] in rec["s"] and wid not in ids:
            rec["s"].remove(scope["id"])
    trend = openalex("/works", {"filter": flt, "group_by": "publication_year"})
    meta["trend"] = {g["key"]: g["count"] for g in (trend or {}).get("group_by", [])
                     if str(g.get("key", "")).isdigit()}
    meta.update({"key": key, "last_fetch": TODAY, "fetched": len(ids), "query": scope["query"]})
    log(f"   {scope['label']}: {total} papers in OpenAlex, {mode} mode, {len(ids)} in the corpus")


def classify_field(corpus, cfg, triager, scopes):
    """Classify pending papers, innermost scope first, within the per-run and AI quota limits."""
    done = 0
    if not triager.available:
        return 0
    queue = []
    for scope in scopes:
        queue += [wid for wid, rec in corpus.items() if scope["id"] in rec["s"] and "k" not in rec and wid not in queue]
    queue = queue[:cfg["max_classify_per_run"]]
    abstracts = fetch_abstracts(queue) if queue else {}
    for wid in queue:
            if not triager.available:
                return done
            rec = corpus[wid]
            result = triager.run(rec["t"][:80], rec["t"], rec["y"], "", abstracts.get(wid, ""), kind="field")
            if triager.exhausted:
                return done
            if not result:
                continue
            rec["k"] = {
                "o": bool(result.get("on_topic")),
                "q": keep(result.get("research_questions"), RESEARCH_QUESTIONS),
                "e": keep(result.get("ecosystems"), ECOSYSTEMS),
                "g": keep(result.get("gases"), GASES),
                "b": result.get("biome") if result.get("biome") in BIOMES else "Not stated",
                "z": result.get("study_design") if result.get("study_design") in STUDY_DESIGNS else None,
                "l": [s for s in (result.get("sites") or []) if isinstance(s, dict)][:5],
                "na": not abstracts.get(wid),      # classified from the title only
                "m": result.get("_model"), "h": result.get("_prompt"), "dt": TODAY,
            }
            done += 1
    return done


def _norm_place(x):
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", x or "") if not unicodedata.combining(ch)).strip().lower()


GEO_LEVELS = CONFIG.get("geography") or [{"id": "world", "label": "World"}]


def in_geo(cc, st, level):
    if not level.get("country_code"):
        return True
    if (cc or "") != level["country_code"].lower():
        return False
    return not level.get("state") or _norm_place(st) == _norm_place(level["state"])


def backfill_library_geo(state):
    """Library sites located by older versions get their country and province."""
    for pid, pts in (state.get("sites") or {}).items():
        for pt in pts:
            if "cc" in pt:
                continue
            parts = [x.strip() for x in pt["label"].split(",") if x.strip()]
            for i in range(len(parts)):
                res = geocode(", ".join(parts[i:]), state)
                if res:
                    pt["cc"], pt["state"] = res[2], (res[3] if i < len(parts) - 1 or len(parts) == 1 else "")
                    break


def geocode_field(corpus, cfg, state):
    _geo_budget["left"] = cfg["max_new_geocodes_per_run"]
    try:
        for rec in corpus.values():
            k = rec.get("k")
            if not k or not k["o"] or not k["l"]:
                continue
            if "p" in k and all(len(pt) >= 5 for pt in k["p"]):
                continue                     # already located with country and province
            k["p"] = [[round(pt["lat"], 3), round(pt["lon"], 3), pt["precision"][0], pt["cc"], pt["state"]]
                      for pt in locate_sites(k["l"], state)]
    except GeocodeBudgetSpent:
        pass
    finally:
        _geo_budget["left"] = None


def wilson(k, n, z=1.96, population=None):
    """Wilson 95% interval, with a finite population correction when the sample is a sizeable share of the scope."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    if population and population > n:
        z = z * math.sqrt((population - n) / (population - 1))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (p, max(0.0, c - h), min(1.0, c + h))


def field_stats(corpus, scopes, state, papers):
    lib_ids = {short_id(p["work"]["id"]) for p in papers if p.get("work")}
    out = []
    for scope in scopes:
        meta = state.get("field_scopes", {}).get(scope["id"], {})
        members = [rec for rec in corpus.values() if scope["id"] in rec["s"]]
        member_ids = {wid for wid, rec in corpus.items() if scope["id"] in rec["s"]}
        classified = [r for r in members if "k" in r]
        on = [r for r in classified if r["k"]["o"]]
        total = meta.get("total", 0)
        on_rate = len(on) / len(classified) if classified else None
        est_on = round(total * on_rate) if on_rate is not None else None
        scale = (est_on / len(on)) if on and est_on else 0

        def dist(values, getter):
            res = {}
            for v in values:
                k_ = sum(1 for r in on if v in getter(r))
                p, lo, hi = wilson(k_, len(on), population=est_on if meta.get("mode") == "sample" else None)
                res[v] = {"n": k_, "p": round(p, 4), "lo": round(lo, 4), "hi": round(hi, 4),
                          "est": round(k_ * scale)}
            return res

        eco_rq = {}
        for gas in ["All"] + GASES:
            sub = on if gas == "All" else [r for r in on if gas in r["k"]["g"]]
            eco_rq[gas] = {e: {q: round(sum(1 for r in sub if e in r["k"]["e"] and q in r["k"]["q"]) * scale)
                               for q in RESEARCH_QUESTIONS} for e in ECOSYSTEMS}
        design_biome = {d: {b: round(sum(1 for r in on if r["k"]["z"] == d and r["k"]["b"] == b) * scale)
                            for b in BIOMES} for d in STUDY_DESIGNS}
        points = [[pt[0], pt[1], pt[2], (r["t"] or "")[:110], r["y"], r.get("d"),
                   pt[3] if len(pt) > 3 else "", pt[4] if len(pt) > 4 else ""]
                  for r in on for pt in r["k"].get("p", [])][:4000]
        located = [r for r in on if any(len(pt) >= 5 for pt in r["k"].get("p", []))]
        geo = {}
        for level in GEO_LEVELS:
            sub = on if not level.get("country_code") else \
                [r for r in located if any(in_geo(pt[3], pt[4], level) for pt in r["k"]["p"] if len(pt) >= 5)]
            rq = {}
            for q in RESEARCH_QUESTIONS:
                k_ = sum(1 for r in sub if q in r["k"]["q"])
                p, lo, hi = wilson(k_, len(sub))
                rq[q] = {"n": k_, "p": round(p, 4), "lo": round(lo, 4), "hi": round(hi, 4), "est": round(k_ * scale)}
            mats = {}
            for gas in ["All"] + GASES:
                g_sub = sub if gas == "All" else [r for r in sub if gas in r["k"]["g"]]
                mats[gas] = {e: {q: round(sum(1 for r in g_sub if e in r["k"]["e"] and q in r["k"]["q"]) * scale)
                                 for q in RESEARCH_QUESTIONS} for e in ECOSYSTEMS}
            geo[level["id"]] = {"n": len(sub), "est": round(len(sub) * scale), "rq": rq, "eco_rq": mats,
                                "design_biome": {d: {b: round(sum(1 for r in sub if r["k"]["z"] == d and r["k"]["b"] == b) * scale)
                                                     for b in BIOMES} for d in STUDY_DESIGNS}}
        out.append({
            "id": scope["id"], "label": scope["label"], "query": scope["query"],
            "mode": meta.get("mode", "full"), "total": total, "fetched": len(members),
            "classified": len(classified), "on_topic": len(on), "est_on_topic": est_on, "scale": round(scale, 3),
            "title_only": sum(1 for r in classified if r["k"].get("na")),
            "rq": dist(RESEARCH_QUESTIONS, lambda r: r["k"]["q"]),
            "gases": dist(GASES, lambda r: r["k"]["g"]),
            "ecosystems": dist(ECOSYSTEMS, lambda r: r["k"]["e"]),
            "eco_rq": eco_rq, "design_biome": design_biome,
            "years": meta.get("trend", {}), "points": points,
            "located": len(located), "geo": geo,
            "library_in_scope": sorted(lib_ids & member_ids),
        })
    return out


def run_field_map(papers, triager, state):
    cfg = load_scopes()
    if not cfg or not cfg.get("scopes"):
        log("   no scopes.json, skipping the field map")
        return None, 0, ""
    scopes = cfg["scopes"]
    corpus = load_corpus()
    for scope in scopes:
        fetch_scope(scope, cfg, corpus, state)
    done = classify_field(corpus, cfg, triager, scopes)
    geocode_field(corpus, cfg, state)
    save_corpus(corpus)
    stats = field_stats(corpus, scopes, state, papers)
    progress = "; ".join(f"{s['label']} {s['classified']}/{s['fetched']}" for s in stats)
    log(f"   classified {done} field papers ({progress})")
    return stats, done, progress


# ----------------------------------------------------------------------------
# Step 5: dashboard data, run log
# ----------------------------------------------------------------------------
def find_milestones(papers, suggestions, limit=12):
    """Landmark papers: highly cited and/or cited by several of your papers."""
    lib = [p for p in papers if p.get("work")]
    lib_ids = {short_id(p["work"]["id"]) for p in lib}
    cited_in_lib = defaultdict(int)
    for p in lib:
        for r in p["work"].get("referenced_works") or []:
            cited_in_lib[short_id(r)] += 1
    items = []
    for p in lib:
        w = p["work"]
        wid = short_id(w["id"])
        items.append({"id": wid, "label": p["name"], "title": p["title"], "year": p["year"] or w.get("publication_year"),
                      "citations": w.get("cited_by_count") or 0, "percentile": percentile(w),
                      "in_library": True, "links": cited_in_lib[wid],
                      "note": (p["key_result"] or p["summary"]).split(". ")[0][:260],
                      "doi": w.get("doi")})
    for c in suggestions:
        if "Prior work" not in c["roles"]:
            continue
        w = c["work"]
        items.append({"id": c["id"], "label": f"{first_author(w)} et al., {w.get('publication_year') or 'n.d.'}",
                      "title": w.get("display_name"), "year": w.get("publication_year"),
                      "citations": w.get("cited_by_count") or 0, "percentile": percentile(w),
                      "in_library": False, "links": len(c["cited_by"]),
                      "note": (c.get("summary") or f"Foundational: cited by {len(c['cited_by'])} of your papers.").split(". ")[0][:260],
                      "doi": w.get("doi")})
    for it in items:
        it["landmark"] = math.log10(it["citations"] + 10) * (1 + it["links"]) * (1.3 if (it["percentile"] or 0) >= 99 else 1)
    items = [it for it in items if it["year"]]
    items.sort(key=lambda it: it["landmark"], reverse=True)
    chosen = sorted(items[:limit], key=lambda it: it["year"])
    for it in chosen:
        it.pop("landmark", None)
    return chosen


def prisma_counts(papers, existing, state):
    decided = [i for i in existing.values() if i["decision"] in ("Added to Zotero", "Not relevant")]
    reasons = defaultdict(int)
    for i in existing.values():
        if i["decision"] == "Not relevant":
            reasons[i["reason"] or "No reason given"] += 1
    identified = len(state.get("seen_candidates", []))
    suggested = len(existing)
    added = sum(1 for i in decided if i["decision"] == "Added to Zotero")
    library = len(papers)
    return {
        "identified_automation": identified,
        "removed_by_ranking": max(0, identified - suggested),
        "suggested": suggested,
        "screened": len(decided),
        "pending": sum(1 for i in existing.values() if i["decision"] == "To review"),
        "excluded": len(decided) - added,
        "exclusion_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "retained_network": added,
        "own_searches": max(0, library - added),
        "library": library,
        "retracted": [p["name"] for p in papers if p.get("retracted")],
        "duplicates": sorted({n for n in DUPLICATES}),
    }


def saturation_series(existing):
    """Cumulative number of relevant suggestions (AI relevance >= 4) by date first suggested."""
    by_day = defaultdict(lambda: [0, 0])
    for i in existing.values():
        day = (i["first_suggested"] or TODAY)[:10]
        by_day[day][0] += 1
        if (i["relevance"] or 0) >= 4:
            by_day[day][1] += 1
    series, total, relevant = [], 0, 0
    for day in sorted(by_day):
        total += by_day[day][0]
        relevant += by_day[day][1]
        series.append({"date": day, "suggested": total, "relevant": relevant})
    return series


def library_rows(papers, state):
    sites = state.get("sites", {})
    rows = []
    for p in papers:
        rows.append({
            "name": p["name"], "title": p["title"], "year": p["year"],
            "category": (p["category"] or ["Uncategorized"])[0],
            "rqs": p["rqs"], "ecosystems": p["ecosystems"], "gases": p["gases"],
            "biome": p.get("biome"), "design": p.get("design"),
            "relevance": p.get("relevance"), "read": p["status"] == "Read",
            "oa": short_id((p.get("work") or {}).get("id")),
            "sites": sites.get(p["page_id"], []),
            "doi": (p.get("work") or {}).get("doi") or (f"https://doi.org/{p['doi']}" if p["doi"] else None),
        })
    return rows


def write_run_log(row):
    header = ["date", "library", "candidates_evaluated", "unique_candidates", "suggested_to_date",
              "new_suggestions", "new_relevant", "pending", "retained", "excluded", "decisions",
              "triaged", "extracted", "retractions", "field_classified", "field_progress",
              "drafts_added", "pending_findings", "model"]
    append_csv("review_log.csv", header, [[row.get(k, "") for k in header]])
    if DRY_RUN or not REVIEW_LOG_DS:
        return
    try:
        create_page(REVIEW_LOG_DS, {
            "Run": p_title(f"Run {row['date']}"),
            "Run date": p_date(row["date"]),
            "Library size": p_num(row["library"]),
            "Candidates evaluated": p_num(row["candidates_evaluated"]),
            "Unique candidates to date": p_num(row["unique_candidates"]),
            "Suggested to date": p_num(row["suggested_to_date"]),
            "New suggestions": p_num(row["new_suggestions"]),
            "New relevant": p_num(row["new_relevant"]),
            "Pending review": p_num(row["pending"]),
            "Retained to date": p_num(row["retained"]),
            "Excluded to date": p_num(row["excluded"]),
            "Decisions this run": p_num(row["decisions"]),
            "Papers triaged": p_num(row["triaged"]),
            "Papers extracted": p_num(row["extracted"]),
            "Retractions": p_num(row["retractions"]),
            "Field papers classified": p_num(row.get("field_classified", 0)),
            "Field progress": p_text(row.get("field_progress", "")),
            "Model": p_text(row["model"]),
            "Notes": p_text(row.get("notes", "")),
        })
    except RuntimeError as e:
        log("   could not write to the Review log database (connect your integration to it): " + str(e)[:150])


def export_dataset(corpus_path=None):
    """Citable dataset: the classified field corpus as CSV (no abstracts), with a data dictionary."""
    corpus = load_corpus()
    if not corpus or DRY_RUN:
        return
    import csv
    out = ROOT / "export"
    out.mkdir(exist_ok=True)
    cols = ["openalex_id", "doi", "title", "year", "cited_by", "scopes", "on_topic", "research_questions",
            "ecosystems", "gases", "biome", "study_design", "sites", "geocoded_points", "title_only",
            "model_version", "prompt_version", "classified_on"]
    with (out / "field_corpus.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for wid, r in sorted(corpus.items()):
            k = r.get("k") or {}
            w.writerow([wid, r.get("d") or "", r.get("t", ""), r.get("y") or "", r.get("c", ""), "|".join(r["s"]),
                        "" if not k else int(k["o"]), "|".join(k.get("q", [])), "|".join(k.get("e", [])),
                        "|".join(k.get("g", [])), k.get("b", ""), k.get("z") or "",
                        "|".join(", ".join(x for x in (s.get("name"), s.get("region"), s.get("country")) if x)
                                 for s in k.get("l", [])),
                        "|".join(f"{p[0]},{p[1]}" for p in k.get("p", [])), int(k["na"]) if k else "",
                        k.get("m", ""), k.get("h", ""), k.get("dt", "")])


# ----------------------------------------------------------------------------
# Weekly digest (a Notion page per week)
# ----------------------------------------------------------------------------
def _rt(text, url=None, bold=False):
    t = {"type": "text", "text": {"content": text[:1900]}, "annotations": {"bold": bold}}
    if url:
        t["text"]["link"] = {"url": url}
    return t


def _para(*parts):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": list(parts)}}


def _bullet(*parts):
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": list(parts)}}


def _heading(text):
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [_rt(text)]}}


def _notion_url(page_id):
    return "https://www.notion.so/" + page_id.replace("-", "")


def library_gap_cells(papers):
    return sorted({f"{e} | {q}" for p in papers for e in p["ecosystems"] for q in p["rqs"]})


def write_digest(papers, existing, field, stopping, alerts, state):
    if not DIGEST_DS or DRY_RUN:
        return
    snap = state.get("digest") or {}
    every = (CONFIG.get("digest") or {}).get("every_days", 7)
    last = snap.get("last")
    if last and (dt.date.today() - dt.date.fromisoformat(last)).days < every:
        return
    since = last or "0000"
    known = set(snap.get("library", []))
    new_lib = [p for p in papers if known and p["page_id"] not in known]
    new_sugg = [i for i in (existing or {}).values() if (i["first_suggested"] or "") > since]
    pending = [i for i in (existing or {}).values() if i["decision"] == "To review"]
    pick = sorted(new_sugg if last else pending,
                  key=lambda i: ((i["relevance"] or 0), (i["personal"] or 0), (i["semantic"] or 0)), reverse=True)[:6]
    cells_now = library_gap_cells(papers)
    filled = sorted(set(cells_now) - set(snap.get("cells", []))) if last else []

    blocks = [_para(_rt(f"Since {last}." if last else "First digest: here is where things stand."))]
    blocks.append(_heading("Worth reading" if last else "Top of your screening queue"))
    blocks += [_bullet(_rt(i["title"][:200], _notion_url(i["page_id"])),
                       _rt(f"  relevance {i['relevance'] or '?'}/5" + (f", personal score {i['personal']}" if i["personal"] is not None else "")))
               for i in pick] or [_bullet(_rt("Nothing new this week."))]
    if new_lib:
        blocks.append(_heading(f"Added to your library ({len(new_lib)})"))
        blocks += [_bullet(_rt(p["name"], p.get("notion_url"))) for p in new_lib[:15]]
    if filled:
        blocks.append(_heading("Evidence gaps your library now covers"))
        blocks += [_bullet(_rt(c)) for c in filled[:15]]
    if field:
        blocks.append(_heading("Field map"))
        blocks += [_bullet(_rt(f"{f['label']}: {f['classified']} of {f['fetched']} papers classified"
                               + (f", about {f['est_on_topic']} on-topic papers in the field" if f.get("est_on_topic") else "")))
                   for f in field]
    if stopping:
        blocks.append(_heading("Screening"))
        blocks.append(_para(_rt(f"About {stopping['expected_relevant']} relevant papers are probably still in your queue of "
                                f"{stopping['queue']} (90% upper estimate {stopping['upper_90']}).")))
    if alerts:
        blocks.append(_heading("Alerts"))
        blocks += [_bullet(_rt(a)) for a in alerts]

    try:
        notion("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": DIGEST_DS},
            "properties": {"Week": p_title(f"Week of {TODAY}"), "Date": p_date(TODAY),
                           "New suggestions": p_num(len(new_sugg) if last else len(pending)),
                           "New relevant": p_num(sum(1 for i in (new_sugg if last else pending) if (i["relevance"] or 0) >= 4)),
                           "Library growth": p_num(len(new_lib)), "Gap changes": p_num(len(filled)),
                           "Left in queue": p_num(stopping["expected_relevant"] if stopping else None)},
            "children": blocks[:95],
        })
        log("   weekly digest written to Notion")
    except RuntimeError as e:
        log("   could not write the weekly digest (connect your integration to it): " + str(e)[:150])
        return
    state["digest"] = {"last": TODAY, "library": [p["page_id"] for p in papers], "cells": cells_now}


def write_dashboard(data):
    if not DRY_RUN:
        DATA_DIR.mkdir(exist_ok=True)
        (DATA_DIR / "dashboard.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    template = (ROOT / "graph_template.html").read_text(encoding="utf-8")
    html = template.replace("/*__GRAPH_DATA__*/null", json.dumps(data, ensure_ascii=False))
    out = ROOT / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    g = data["graph"]
    log(f"   wrote docs/index.html ({len(g['nodes'])} nodes, {len(g['edges'])} links, "
        f"{sum(len(r['sites']) for r in data['library'])} mapped sites, {len(data['milestones'])} milestones)")


# ----------------------------------------------------------------------------
def main():
    if not NOTION_TOKEN:
        sys.exit("NOTION_TOKEN is not set.")
    if not OPENALEX_KEY:
        log("Warning: OPENALEX_API_KEY is not set, OpenAlex will only allow a tiny daily quota.")
    state = load_state()
    global PREDICTIONS
    PREDICTIONS = state.setdefault("predictions", {})

    log("Loading library from Notion")
    papers = load_library()
    log(f"   {len(papers)} papers")

    log("1. Citation metrics (OpenAlex)")
    metrics = enrich_metrics(papers)
    DUPLICATES[:] = metrics["duplicates"]

    log("2. AI triage of your papers")
    triager = Triager()
    triaged = triage_papers(papers, triager)

    log("3. Extraction: study sites, design, reported values")
    extracted = extract_papers(papers, triager, state)
    backfill_library_geo(state)

    log("3b. Synthesis matrix: your reviews, new draft findings, reading tiers")
    import synthesis
    synth = {"drafts_added": 0, "pending_review": 0, "tiers": 0}
    try:
        synth = synthesis.run(sys.modules[__name__], papers, triager, state, CONFIG)
    except Exception as e:
        log(f"   synthesis step skipped: {type(e).__name__}: {str(e)[:200]}")

    log("4. Suggested papers")
    try:
        existing = load_existing_suggestions()
    except RuntimeError as e:
        existing = None
        log("   Could not open the Suggested papers database in Notion. Open it, click ••• > Connections")
        log("   and add your integration (this happens when the database is moved). Details: " + str(e)[:200])
    decisions = track_decisions(existing, state) if existing is not None else 0
    model = train_preferences(existing or {})
    if model["ready"]:
        log(f"   learned your preferences from {model['n']} decisions (separation AUC {model['auc']})")
    else:
        log(f"   preference learning waits for more decisions ({model['n']}/{model['needed']}, "
            "with at least 3 Added and 3 Not relevant)")
    # --- semantic layer (local model, no AI quota) ---
    import semantic
    emb = semantic.get_embedder(CONFIG, log)
    sem_extra = {}
    if emb is not None:
        try:
            lib_ids_now = {short_id(p["work"]["id"]) for p in papers if p.get("work")}
            sem_extra = semantic.discover(emb, CONFIG, RESEARCH_QUESTIONS, openalex, abstract_text, short_id,
                                          lib_ids_now, log)
        except Exception as e:
            log(f"   semantic discovery skipped: {type(e).__name__}: {str(e)[:150]}")
    text_model = {"ready": False}
    if emb is not None and existing:
        lib_texts = [semantic.paper_text(p["title"], p["abstract"]) for p in papers]
        kept = [i for i in existing.values() if i["decision"] == "Added to Zotero"]
        dropped = [i for i in existing.values() if i["decision"] == "Not relevant"]
        txt = lambda i: semantic.paper_text(i["title"], i["abstract"] or i["summary"])
        try:
            text_model = semantic.train_text_model(
                emb, lib_texts + [txt(i) for i in kept], [txt(i) for i in dropped],
                [(txt(i), 1) for i in kept] + [(txt(i), 0) for i in dropped])
        except Exception as e:
            log(f"   text model skipped: {type(e).__name__}: {str(e)[:150]}")
        if text_model.get("ready"):
            log(f"   text model trained on {text_model['positives']} kept / {text_model['negatives']} excluded papers"
                f" (cross-validated AUC {text_model.get('auc_cv')})")

    def scorer(title, abstract, rel, sim, conn, pct, year):
        """Personal score: average of the text model and the 5-feature model, whichever are ready."""
        parts = []
        if text_model.get("ready"):
            parts.append(text_model["predict"]([semantic.paper_text(title, abstract)])[0])
        pm = personal_score(model, rel, sim, conn, pct, year)
        if pm is not None:
            parts.append(pm / 100)
        return round(100 * sum(parts) / len(parts)) if parts else None

    top, refresh, lib_ids, candidates = build_suggestions(papers, existing or {}, emb=emb, sem_extra=sem_extra)
    # everything ever suggested was, by definition, identified by the algorithm
    seen = set(state.get("seen_candidates", [])) | set(candidates) | set(existing or {})
    state["seen_candidates"] = sorted(seen)
    stats = {"created": 0, "new_relevant": 0, "triaged": 0}
    if existing is not None:
        stats = sync_suggestions(top, refresh, lib_ids, existing, triager, scorer, emb=emb)
        existing = load_existing_suggestions()   # fresh view including today's additions
    stopping = None
    pending = [i for i in (existing or {}).values() if i["decision"] == "To review"]
    probs = [scorer(i["title"], i["abstract"], i["relevance"], i["similarity"], i["connections"],
                    i["percentile"], i["year"]) for i in pending]
    if pending and all(p is not None for p in probs):
        stopping = semantic.stopping_estimate([p / 100 for p in probs])
        log(f"   about {stopping['expected_relevant']} relevant papers likely left in your queue of {len(pending)}")

    log("5. Field-wide systematic map")
    field, field_done, field_progress = run_field_map(papers, triager, state)

    log("6. Dashboard and review log")
    state["seen_candidates"] = sorted(set(state["seen_candidates"]) | set(existing or {}))
    prisma = prisma_counts(papers, existing or {}, state)
    data = {
        "generated": TODAY,
        "graph": build_graph(papers, top),
        "library": library_rows(papers, state),
        "field": field,
        "milestones": find_milestones(papers, top),
        "prisma": prisma,
        "saturation": saturation_series(existing or {}),
        "preferences": {**model, "features": FEATURES, "prospective": prospective_eval(existing or {}, state),
                        "text_model": {k: v for k, v in text_model.items() if k != "predict"},
                        "semantic_model": semantic.settings(CONFIG)["model"] if emb is not None else None},
        "stopping": stopping,
        "validation": json.loads((DATA_DIR / "validation.json").read_text(encoding="utf-8"))
                      if (DATA_DIR / "validation.json").exists() else {},
        "runs": state.get("runs", [])[-60:],
        "synthesis": {k: synth.get(k) for k in ("drafts_added", "pending_review", "tiers")}
                     | {"stats": state.get("synthesis_stats", {})},
        "options": {"rqs": RESEARCH_QUESTIONS, "ecosystems": ECOSYSTEMS, "gases": GASES,
                    "biomes": BIOMES, "categories": CATEGORIES, "designs": STUDY_DESIGNS,
                    "geography": GEO_LEVELS},
    }
    run = {
        "date": TODAY, "library": len(papers), "candidates_evaluated": len(candidates),
        "unique_candidates": len(seen), "suggested_to_date": prisma["suggested"],
        "new_suggestions": stats["created"], "new_relevant": stats["new_relevant"],
        "pending": prisma["pending"], "retained": prisma["retained_network"], "excluded": prisma["excluded"],
        "decisions": decisions, "triaged": (triaged or 0) + stats["triaged"], "extracted": extracted,
        "retractions": len(metrics["retractions"]), "model": triager.model,
        "field_classified": field_done, "field_progress": field_progress,
        "drafts_added": synth.get("drafts_added", 0), "pending_findings": synth.get("pending_review", 0),
        "notes": ("Retracted in library: " + "; ".join(metrics["retractions"])) if metrics["retractions"] else "",
    }
    state.setdefault("runs", []).append(run)
    data["runs"] = state["runs"][-60:]
    state.setdefault("prompts", {}).update(_PROMPTS_SEEN)   # every prompt version ever used, archived
    write_dashboard(data)
    alerts = ([f"Retracted: {r}" for r in metrics["retractions"]]
              + [f"Duplicate in your library: {d} (merge it in Zotero)" for d in metrics["duplicates"]])
    if synth.get("pending_review"):
        alerts.append(f"{synth['pending_review']} draft findings wait in your Synthesis matrix review queue")
    write_digest(papers, existing, field, stopping, alerts, state)
    export_dataset()
    write_run_log(run)
    save_state(state)
    log("Done.")


if __name__ == "__main__":
    main()
