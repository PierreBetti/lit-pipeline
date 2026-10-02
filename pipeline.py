#!/usr/bin/env python3
"""
lit-pipeline: nightly enrichment of the Notero (Zotero -> Notion) database.

  1. Metrics      citation count, field-normalized citation percentile and FWCI (OpenAlex)
  2. Triage       AI summary, relevance to the PhD, research questions, ecosystem, gases,
                  methods, key result, and Category when it is still empty
                  (Gemini API free tier by default)
  3. Suggestions  papers strongly connected to your library but not in it yet
                  (OpenAlex citation links + Semantic Scholar recommendations)
  4. Graph        interactive citation map written to docs/index.html (GitHub Pages)

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

MAX_TRIAGE_PER_RUN = int(os.environ.get("MAX_TRIAGE_PER_RUN", "40"))
MAX_SUGGESTIONS = int(os.environ.get("MAX_SUGGESTIONS", "40"))
MIN_CONNECTIONS = int(os.environ.get("MIN_CONNECTIONS", "2"))
GRAPH_SUGGESTIONS = int(os.environ.get("GRAPH_SUGGESTIONS", "25"))
MAX_SUGGESTION_TRIAGE_PER_RUN = int(os.environ.get("MAX_SUGGESTION_TRIAGE_PER_RUN", "20"))
CANDIDATE_POOL = int(os.environ.get("CANDIDATE_POOL", "150"))   # candidates scored in depth each run
CITER_PAGES = int(os.environ.get("CITER_PAGES", "2"))           # pages of 200 citing papers per batch

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
WORK_FIELDS = ("id,doi,display_name,publication_year,type,cited_by_count,fwci,"
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
        if new != old or p["openalex_url"] != w["id"]:
            props = {k: p_num(v) for k, v in new.items()}
            props["OpenAlex ID"] = p_url(w["id"])
            update_page(p["page_id"], props)
    log(f"   {found}/{len(papers)} papers matched on OpenAlex")


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


def ask_gemini(system, user):
    """Call Gemini, falling back to the next model when one has no free quota for this key."""
    global GEMINI_MODEL
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_schema(TRIAGE_TOOL["input_schema"]),
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


def ask_claude(client, system, user):
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1200,
        system=system,
        tools=[TRIAGE_TOOL],
        tool_choice={"type": "tool", "name": "record_triage"},
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
        if self.provider == "gemini":
            self.system = self.system.replace("Record your answer with the record_triage tool.",
                                              "Answer in the requested JSON format.")

    @property
    def available(self):
        return self.provider != "none" and not self.exhausted

    def run(self, label, title, year, journal, abstract):
        if not self.available:
            return None
        user = (f"Title: {title}\nYear: {year or 'unknown'}\n"
                f"Journal: {journal or 'unknown'}\n"
                f"Abstract: {abstract or '(no abstract available)'}")
        try:
            if self.provider == "gemini":
                result = ask_gemini(self.system, user)
                time.sleep(GEMINI_SECONDS_BETWEEN_CALLS)
            else:
                result = ask_claude(self.client, self.system, user)
        except GeminiQuotaError as e:
            log(f"   AI quota reached ({e}), the rest waits for the next run")
            self.exhausted = True
            return None
        except Exception as e:
            log(f"   AI error on {label}: {e}")
            return None
        if not result:
            log(f"   no triage returned for {label}")
            return None
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
        return
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
        done += 1
    left = max(0, len(todo) - done)
    log(f"   triaged {done} papers" + (f", {left} left for the next runs" if left else ""))


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
            existing[oid] = {"page_id": page["id"], "decision": read(pr.get("Decision")),
                             "relevance": read(pr.get("Relevance")), "triage_date": read(pr.get("Triage date"))}
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
        c["global"] = global_score(c["relevance"], c["similarity"], c["score"],
                                   percentile(w), w.get("publication_year"))

    scored.sort(key=lambda c: c["global"], reverse=True)
    top = scored[:MAX_SUGGESTIONS]
    top_ids = {c["id"] for c in top}
    # also refresh suggestions already in Notion that are still in the scored pool
    refresh = [c for c in scored if c["id"] in existing and c["id"] not in top_ids]
    log(f"   {len(candidates)} connected papers found, {len(scored)} scored in depth, "
        f"keeping the top {len(top)}")
    return top, refresh, lib_ids


def sync_suggestions(top, refresh, lib_ids, existing, triager):
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

    # AI relevance for suggestions, best candidates first, within the free quota left today
    if not triager.available:
        if triager.provider != "none":
            log("   suggestion triage postponed (AI quota used up today)")
        return
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
        c["global"] = global_score(c["relevance"], c["similarity"], c["score"],
                                   percentile(w), w.get("publication_year"))
        done += 1
    left = max(0, len(to_triage) - done)
    log(f"   AI-scored {done} suggestions" + (f", {left} left for the next runs" if left else ""))


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

    data = {"generated": TODAY, "nodes": nodes, "edges": edges}
    template = (ROOT / "graph_template.html").read_text(encoding="utf-8")
    html = template.replace("/*__GRAPH_DATA__*/null", json.dumps(data, ensure_ascii=False))
    out = ROOT / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log(f"   wrote docs/index.html ({len(nodes)} nodes, {len(edges)} links)")


# ----------------------------------------------------------------------------
def main():
    if not NOTION_TOKEN:
        sys.exit("NOTION_TOKEN is not set.")
    if not OPENALEX_KEY:
        log("Warning: OPENALEX_API_KEY is not set, OpenAlex will only allow a tiny daily quota.")

    log("Loading library from Notion")
    papers = load_library()
    log(f"   {len(papers)} papers")

    log("1. Citation metrics (OpenAlex)")
    enrich_metrics(papers)

    log("2. AI triage of your papers")
    triager = Triager()
    triage_papers(papers, triager)

    log("3. Suggested papers")
    try:
        existing = load_existing_suggestions()
    except RuntimeError as e:
        existing = None
        log("   Could not open the Suggested papers database in Notion. Open it, click ••• > Connections")
        log("   and add your integration (this happens when the database is moved). Details: " + str(e)[:200])
    top, refresh, lib_ids = build_suggestions(papers, existing or {})
    if existing is not None:
        sync_suggestions(top, refresh, lib_ids, existing, triager)

    log("4. Literature map")
    build_graph(papers, top)
    log("Done.")


if __name__ == "__main__":
    main()
