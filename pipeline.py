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

# OpenAlex search describing your field, used for the "field vs your library" timeline.
FIELD_QUERY = os.environ.get("FIELD_QUERY",
    '(swamp OR "forested wetland" OR "forested wetlands") AND (methane OR "carbon dioxide" OR "nitrous oxide" OR "greenhouse gas")')

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
               "primary_location,abstract_inverted_index")


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
    return {"retractions": [p["name"] for p in papers if p["retracted"]]}


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
            "temperature": 0.2,
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
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
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
        max_tokens=1500,
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
        self.calls = 0

    @property
    def model(self):
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
        system, tool = (self.system, TRIAGE_TOOL) if kind == "triage" else (self.extract_system, EXTRACT_TOOL)
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


def geocode(query, state):
    """Free geocoding with OpenStreetMap Nominatim (max 1 request per second), cached between runs."""
    cache = state.setdefault("geocode", {})
    if query in cache:
        return cache[query]
    time.sleep(1.1)
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": query, "format": "json", "limit": 1},
                         headers={"User-Agent": f"lit-pipeline/1.0 ({CONTACT_EMAIL or 'personal research tool'})"},
                         timeout=30)
        hits = r.json() if r.ok else []
    except Exception:
        hits = []
    result = [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None
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
                               "lat": coords[0], "lon": coords[1], "precision": precision})
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


def global_score(relevance, similarity, connections, pct, year):
    """Same formula as the 'Global score' property in Notion. If you change one, change the other."""
    return round(100 * (
        0.35 * (relevance / 5 if relevance else 0.5)
        + 0.30 * (similarity or 0) / 100
        + 0.15 * min(connections or 0, 5) / 5
        + 0.12 * (pct / 100 if pct is not None else 0.5)
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
            }
    return existing


def build_suggestions(papers, existing):
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
    candidates = (set(cited_by_lib) | set(cites_lib) | set(cocite) | s2) - lib_ids
    pre = {x: 2 * len(cited_by_lib[x]) + 2 * len(cites_lib[x]) + sum(cocite[x].values()) + (2 if x in s2 else 0)
           for x in candidates}
    pool = [x for x, v in sorted(pre.items(), key=lambda kv: kv[1], reverse=True) if v >= 2][:CANDIDATE_POOL]

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
                                   percentile(w), w.get("publication_year"))

    scored.sort(key=lambda c: c["global"], reverse=True)
    top = scored[:MAX_SUGGESTIONS]
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
    w = [0.0] * (len(FEATURES) + 1)          # last weight is the intercept
    lr, l2 = 0.5, 0.05
    for _ in range(4000):
        grad = [0.0] * len(w)
        for xi, yi in zip(X, y):
            err = _sigmoid(sum(a * b for a, b in zip(w, xi + [1]))) - yi
            for j, v in enumerate(xi + [1]):
                grad[j] += err * v
        for j in range(len(w)):
            reg = l2 * w[j] if j < len(FEATURES) else 0
            w[j] -= lr * (grad[j] / len(y) + reg)
    # in-sample AUC: how well the learned score separates what you kept from what you excluded
    scores = [sum(a * b for a, b in zip(w, xi + [1])) for xi in X]
    pos = [s for s, t in zip(scores, y) if t]
    neg = [s for s, t in zip(scores, y) if not t]
    auc = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg))
    return {"ready": True, "n": len(y), "added": n_pos, "excluded": n_neg, "weights": w[:-1],
            "intercept": w[-1], "auc": round(auc, 2)}


def personal_score(model, relevance, similarity, connections, pct, year):
    if not model.get("ready"):
        return None
    x = feature_vector(relevance, similarity, connections, pct, year)
    return round(100 * _sigmoid(sum(a * b for a, b in zip(model["weights"], x)) + model["intercept"]))


def track_decisions(existing, state):
    """Compare decisions with last run's snapshot and log every change (the screening audit trail)."""
    snap = state.setdefault("decisions", {})
    events = []
    for oid, info in existing.items():
        current = [info["decision"] or "", info["reason"] or ""]
        if snap.get(oid) != current:
            if snap.get(oid) is not None or info["decision"] != "To review":
                events.append([TODAY, oid, info["title"][:200], (snap.get(oid) or ["", ""])[0],
                               current[0], current[1]])
            snap[oid] = current
    append_csv("decisions.csv", ["date", "openalex_id", "title", "previous_decision", "decision",
                                 "exclusion_reason"], events)
    return len(events)


def sync_suggestions(top, refresh, lib_ids, existing, triager, model):
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
        ps = personal_score(model, c["relevance"], c["similarity"], c["score"], percentile(w),
                            w.get("publication_year"))
        if ps is not None:
            props["Personal score"] = p_num(ps)
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

    # Personal score for older pending suggestions that were not re-scored this run
    if model.get("ready"):
        scored_ids = {c["id"] for c in top + refresh}
        for oid, info in existing.items():
            if oid in scored_ids or info["decision"] != "To review":
                continue
            ps = personal_score(model, info["relevance"], info["similarity"], info["connections"],
                                info["percentile"], info["year"])
            if ps != info["personal"]:
                update_page(info["page_id"], {"Personal score": p_num(ps)})

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
                                   percentile(w), w.get("publication_year"))
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

    def add_edge(a, b, kind):
        key = (a, b, kind) if kind == "cites" else (tuple(sorted((a, b))), kind)
        if key not in seen:
            seen.add(key)
            edges.append({"from": a, "to": b, "kind": kind})

    for p in lib:
        w = p["work"]
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

    shown = sorted(suggestions, key=lambda c: c["global"], reverse=True)[:GRAPH_SUGGESTIONS]
    for c in shown:
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
# Step 5: dashboard data, run log
# ----------------------------------------------------------------------------
def field_trend():
    """Papers per year in the whole field (OpenAlex search), to compare with your library."""
    if not FIELD_QUERY:
        return {}
    res = openalex("/works", {"filter": f"title_and_abstract.search:{FIELD_QUERY}",
                              "group_by": "publication_year"})
    out = {}
    for g in (res or {}).get("group_by", []):
        try:
            year = int(g["key"])
        except (TypeError, ValueError):
            continue
        if 1960 <= year <= dt.date.today().year:
            out[year] = g["count"]
    return out


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
            "sites": sites.get(p["page_id"], []),
            "doi": (p.get("work") or {}).get("doi") or (f"https://doi.org/{p['doi']}" if p["doi"] else None),
        })
    return rows


def write_run_log(row):
    header = ["date", "library", "candidates_evaluated", "unique_candidates", "suggested_to_date",
              "new_suggestions", "new_relevant", "pending", "retained", "excluded", "decisions",
              "triaged", "extracted", "retractions", "model"]
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
            "Model": p_text(row["model"]),
            "Notes": p_text(row.get("notes", "")),
        })
    except RuntimeError as e:
        log("   could not write to the Review log database (connect your integration to it): " + str(e)[:150])


def write_dashboard(data):
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

    log("Loading library from Notion")
    papers = load_library()
    log(f"   {len(papers)} papers")

    log("1. Citation metrics (OpenAlex)")
    metrics = enrich_metrics(papers)

    log("2. AI triage of your papers")
    triager = Triager()
    triaged = triage_papers(papers, triager)

    log("3. Extraction: study sites, design, reported values")
    extracted = extract_papers(papers, triager, state)

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
    top, refresh, lib_ids, candidates = build_suggestions(papers, existing or {})
    # everything ever suggested was, by definition, identified by the algorithm
    seen = set(state.get("seen_candidates", [])) | set(candidates) | set(existing or {})
    state["seen_candidates"] = sorted(seen)
    stats = {"created": 0, "new_relevant": 0, "triaged": 0}
    if existing is not None:
        stats = sync_suggestions(top, refresh, lib_ids, existing, triager, model)
        existing = load_existing_suggestions()   # fresh view including today's additions

    log("5. Dashboard and review log")
    state["seen_candidates"] = sorted(set(state["seen_candidates"]) | set(existing or {}))
    prisma = prisma_counts(papers, existing or {}, state)
    data = {
        "generated": TODAY,
        "graph": build_graph(papers, top),
        "library": library_rows(papers, state),
        "field_trend": field_trend(),
        "field_query": FIELD_QUERY,
        "milestones": find_milestones(papers, top),
        "prisma": prisma,
        "saturation": saturation_series(existing or {}),
        "preferences": {**model, "features": FEATURES},
        "runs": state.get("runs", [])[-60:],
        "options": {"rqs": RESEARCH_QUESTIONS, "ecosystems": ECOSYSTEMS, "gases": GASES,
                    "biomes": BIOMES, "categories": CATEGORIES},
    }
    run = {
        "date": TODAY, "library": len(papers), "candidates_evaluated": len(candidates),
        "unique_candidates": len(seen), "suggested_to_date": prisma["suggested"],
        "new_suggestions": stats["created"], "new_relevant": stats["new_relevant"],
        "pending": prisma["pending"], "retained": prisma["retained_network"], "excluded": prisma["excluded"],
        "decisions": decisions, "triaged": (triaged or 0) + stats["triaged"], "extracted": extracted,
        "retractions": len(metrics["retractions"]), "model": triager.model,
        "notes": ("Retracted in library: " + "; ".join(metrics["retractions"])) if metrics["retractions"] else "",
    }
    state.setdefault("runs", []).append(run)
    data["runs"] = state["runs"][-60:]
    write_dashboard(data)
    write_run_log(run)
    save_state(state)
    log("Done.")


if __name__ == "__main__":
    main()
