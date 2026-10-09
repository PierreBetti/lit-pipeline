"""
Synthesis matrix: AI-drafted findings that you review, plus suggested reading tiers.

How it fits together (all in Notion, under Literature Matrix > Synthesis matrix):
  Themes        your themes, each with a working claim (the statement findings support or counter)
  Findings      accepted findings: claim + paper + theme + stance (+ page and quote). Only these count.
  Review queue  AI drafts waiting for you: Accept (optionally after editing) or Reject.

Each night:
  1. process_reviews()   Accepted drafts become Findings (marked "AI draft (as is)" or "AI draft (edited)"),
                         Rejected drafts are moved to the Notion trash. Every decision is logged
                         (logs/findings_review.csv) so the extraction accuracy can be reported.
  2. draft_findings()    For a few papers you marked Read that have no findings and no drafts yet,
                         read the full text (your Zotero PDF, else an open-access PDF, else the abstract)
                         and draft findings against your themes. Quotes are checked word for word
                         against the text and the page number is derived from where the quote sits.
  2b. restance()        When you edit a theme's working claim (or move a finding to another theme), the stances
                         of its findings and drafts are re-judged against the new claim, from the finding text
                         alone. Changes are written to "Stance note" and logs/stance_updates.csv.
  3. suggest_tiers()     Fill Tier (Core / Supporting / Background) where it is still empty. Never overwrites.

Full texts are read in memory only. Nothing from them is stored in the repository
except the short quotes you see in Notion (the repository may be public).
"""

import io
import json
import os
import re
import time
import unicodedata

import requests

ZOTERO_KEY = os.environ.get("ZOTERO_API_KEY", "")
ZOTERO_USER = os.environ.get("ZOTERO_USER_ID", "")
ZOTERO_API = "https://api.zotero.org"

STANCES = ["Supports", "Counters", "Mixed", "Describes"]
SOURCE_ZOTERO, SOURCE_OA, SOURCE_ABSTRACT = "Full text (Zotero)", "Full text (open access)", "Abstract only"
UNVERIFIED = "⚠ not found verbatim in the text: "


def settings(config):
    return {"max_papers_per_run": 3, "max_findings_per_paper": 8, "max_chars": 60000,
            "suggest_tiers": True, **(config.get("synthesis") or {})}


# ----------------------------------------------------------------------------
# Full text
# ----------------------------------------------------------------------------
def pdf_text(data):
    """PDF bytes -> text with [page N] markers, or '' if unreadable (scanned, encrypted...)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for i, page in enumerate(reader.pages, start=1):
            t = page.extract_text() or ""
            if t.strip():
                parts.append(f"[page {i}]\n{t}")
        return "\n".join(parts)
    except Exception:
        return ""


def zotero_item_path(zotero_uri):
    """'zotero://select/library/items/ABCD1234' or '.../groups/123/items/ABCD1234' -> API path."""
    m = re.search(r"items/([A-Z0-9]{8})", zotero_uri or "")
    if not m:
        return None
    g = re.search(r"groups/(\d+)", zotero_uri)
    if g:
        return f"/groups/{g.group(1)}/items/{m.group(1)}"
    return f"/users/{ZOTERO_USER}/items/{m.group(1)}" if ZOTERO_USER else None


def zotero_full_text(zotero_uri):
    if not (ZOTERO_KEY and zotero_uri):
        return ""
    path = zotero_item_path(zotero_uri)
    if not path:
        return ""
    headers = {"Zotero-API-Key": ZOTERO_KEY, "Zotero-API-Version": "3"}
    try:
        r = requests.get(f"{ZOTERO_API}{path}/children", headers=headers, timeout=60)
        if not r.ok:
            return ""
        base = path.rsplit("/items/", 1)[0]
        pdfs = [c["key"] for c in r.json()
                if c.get("data", {}).get("itemType") == "attachment"
                and c["data"].get("contentType") == "application/pdf"]
        for key in pdfs:
            f = requests.get(f"{ZOTERO_API}{base}/items/{key}/file", headers=headers, timeout=120)
            if f.ok and f.content[:4] == b"%PDF":
                text = pdf_text(f.content)
                if text:
                    return text
            # file not stored on zotero.org (e.g. linked file or WebDAV): use Zotero's own index
            ft = requests.get(f"{ZOTERO_API}{base}/items/{key}/fulltext", headers=headers, timeout=60)
            if ft.ok and ft.json().get("content"):
                return ft.json()["content"]
    except (requests.RequestException, ValueError):
        return ""
    return ""


def open_access_text(work):
    loc = (work or {}).get("best_oa_location") or {}
    url = loc.get("pdf_url")
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=120, headers={"User-Agent": "lit-pipeline (academic use)"})
        if r.ok and r.content[:4] == b"%PDF":
            return pdf_text(r.content)
    except requests.RequestException:
        pass
    return ""


def get_text(paper, max_chars):
    text = zotero_full_text(paper.get("zotero_uri"))
    if len(text) > 2000:
        return text[:max_chars], SOURCE_ZOTERO
    text = open_access_text(paper.get("work"))
    if len(text) > 2000:
        return text[:max_chars], SOURCE_OA
    return (paper.get("abstract") or ""), SOURCE_ABSTRACT


# ----------------------------------------------------------------------------
# Quote verification
# ----------------------------------------------------------------------------
def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = s.replace("-\n", "").replace("­", "")       # words hyphenated across lines
    s = re.sub(r"[^\w]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def locate_quote(quote, text):
    """(found, page) for a quote, tolerant to line breaks, hyphenation and punctuation."""
    q = _norm(quote)
    if len(q) < 12:
        return False, None
    pages = re.split(r"\[page (\d+)\]", text)
    if len(pages) > 1:   # ['', '1', text1, '2', text2, ...]
        for i in range(1, len(pages) - 1, 2):
            if q in _norm(pages[i + 1]):
                return True, int(pages[i])
        # a quote spanning a page break
        if q in _norm(re.sub(r"\[page \d+\]", " ", text)):
            return True, None
        return False, None
    return (q in _norm(text)), None


# ----------------------------------------------------------------------------
# AI drafting
# ----------------------------------------------------------------------------
FINDINGS_TOOL = {
    "name": "record_findings",
    "description": "Record the findings of one paper, each tied to a theme of the thesis.",
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding": {"type": "string",
                                    "description": "One claim the paper supports with its own evidence, paraphrased in one or two sentences, with numbers when the paper gives them."},
                        "theme": {"type": "string",
                                  "description": "Exact name of the closest existing theme, or empty if none fits."},
                        "new_theme": {"type": "string",
                                      "description": "Short name for a new theme, only when no existing theme fits. Otherwise empty."},
                        "stance": {"type": "string", "enum": STANCES,
                                   "description": "Relative to the theme's working claim."},
                        "locator": {"type": "string",
                                    "description": "Section where the evidence is (e.g. Results, Fig. 3, Table 2)."},
                        "quote": {"type": "string",
                                  "description": "Short verbatim passage (under 40 words) copied exactly from the text, supporting the finding."},
                    },
                    "required": ["finding", "theme", "new_theme", "stance", "locator", "quote"],
                },
            }
        },
        "required": ["findings"],
    },
}

FINDINGS_SYSTEM = """You help a PhD student build a synthesis matrix for a literature review.

Research context:
{context}

The thesis is organized in themes. Each theme has a working claim:
{themes}

Read the paper and list its findings: the claims it supports with its own data or analysis (for a review, the
conclusions it draws from the literature). At most {max_findings}, the most important first. Skip background
statements the paper only cites from others.

Rules:
- Paraphrase each finding in one or two sentences, keeping the numbers, units, site and conditions.
- theme: copy the exact name of the closest theme above. If none fits, leave theme empty and give a short new_theme.
- stance, relative to that theme's working claim: Supports, Counters, Mixed (supports under some conditions and
  counters under others), or Describes (relevant but takes no side).
- quote: copy a passage of under 40 words exactly as written in the text, so it can be found again. Never
  reconstruct or merge sentences.
- Only the text provided counts. Never invent results. If only the abstract is available, keep to what it says.
Record your answer with the record_findings tool."""


class Findings:
    def __init__(self, P, triager, themes, cfg):
        self.P, self.triager, self.cfg = P, triager, cfg
        context = (P.ROOT / "research_context.md").read_text(encoding="utf-8")
        lines = [f"- {t['name']}: {t['claim'] or '(no working claim yet)'}" for t in themes]
        self.system = FINDINGS_SYSTEM.format(context=context, themes="\n".join(lines) or "(no themes yet)",
                                             max_findings=cfg["max_findings_per_paper"])
        if triager.provider == "gemini":
            self.system = self.system.replace("Record your answer with the record_findings tool.",
                                              "Answer in the requested JSON format.")
        self.phash = P.prompt_hash(self.system, FINDINGS_TOOL)
        P._PROMPTS_SEEN[self.phash] = {"kind": "findings", "system": self.system, "schema": FINDINGS_TOOL}

    def draft(self, paper, text, source):
        P, t = self.P, self.triager
        if not t.available:
            return None
        user = (f"Title: {paper['title']}\nYear: {paper.get('year') or 'unknown'}\n"
                f"Journal: {paper.get('journal') or 'unknown'}\nText available: {source}\n\n{text}")
        try:
            t.calls += 1
            if t.provider == "gemini":
                result = P.ask_gemini(self.system, user, FINDINGS_TOOL)
                time.sleep(P.GEMINI_SECONDS_BETWEEN_CALLS)
            else:
                result = P.ask_claude(t.client, self.system, user, FINDINGS_TOOL)
        except P.GeminiQuotaError as e:
            P.log(f"   AI quota reached ({e}), drafting continues next run")
            t.exhausted = True
            return None
        except Exception as e:
            P.log(f"   AI error while drafting {paper['name']}: {str(e)[:200]}")
            return None
        if not result:
            return None
        version = result.pop("_model", None) or t.model
        t.last_version = version
        P.log_ai_call("findings", paper["name"], version, self.phash, result)
        return result.get("findings") or []


RESTANCE_TOOL = {
    "name": "record_stances",
    "description": "Record the stance of each finding relative to the working claim.",
    "input_schema": {
        "type": "object",
        "properties": {
            "stances": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer", "description": "Number of the finding in the list."},
                        "stance": {"type": "string", "enum": STANCES},
                    },
                    "required": ["n", "stance"],
                },
            }
        },
        "required": ["stances"],
    },
}

RESTANCE_SYSTEM = """You help a PhD student keep a literature synthesis matrix consistent.

For each numbered finding below, judge its stance relative to the working claim of the theme:
- Supports: the finding is evidence for the claim.
- Counters: the finding is evidence against the claim.
- Mixed: it supports the claim under some conditions and counters it under others.
- Describes: relevant to the theme but takes no side on the claim.
Judge only from the finding as written. Do not assume anything the finding does not state.
Record your answer with the record_stances tool."""


def _claim_key(claim):
    return _norm((claim or "")[:1990])


def restance(P, triager, themes, ids, state, max_calls=6):
    """Re-judge stances whose 'Claim used' no longer matches their theme's working claim."""
    claims = {t["id"]: t["claim"] for t in themes}
    names = {t["id"]: t["name"] for t in themes}
    system = RESTANCE_SYSTEM
    if triager.provider == "gemini":
        system = system.replace("Record your answer with the record_stances tool.", "Answer in the requested JSON format.")
    phash = P.prompt_hash(system, RESTANCE_TOOL)
    P._PROMPTS_SEEN[phash] = {"kind": "restance", "system": system, "schema": RESTANCE_TOOL}
    todo = {}            # theme id -> list of (page id, finding, old stance)
    stamped = 0
    for ds in (ids["findings"], ids["review"]):
        for page in P.query_all(ds):
            pr = page["properties"]
            if ds == ids["review"] and P.read(pr.get("Decision")) not in (None, "To review"):
                continue
            th = rel_ids(pr.get("Theme"))
            claim = claims.get(th[0]) if th else ""
            if not claim:
                continue
            used = P.read(pr.get("Claim used")) or ""
            if _claim_key(used) == _claim_key(claim):
                continue
            if not used and P.read(pr.get("Origin")) == "Written by me":
                # your own finding: its stance was set by you against the current claim
                P.update_page(page["id"], {"Claim used": P.p_text(claim)})
                stamped += 1
                continue
            todo.setdefault(th[0], []).append((page["id"], P.read(pr.get("Finding")) or "",
                                               P.read(pr.get("Stance"))))
    changed, rows, calls = 0, [], 0
    for tid, items in todo.items():
        for start in range(0, len(items), 40):
            if not triager.available or calls >= max_calls:
                P.log("   stance updates continue next run")
                break
            batch = items[start:start + 40]
            user = (f"Theme: {names[tid]}\nWorking claim: {claims[tid]}\n\nFindings:\n"
                    + "\n".join(f"{i + 1}. {f}" for i, (_, f, _) in enumerate(batch)))
            calls += 1
            try:
                triager.calls += 1
                if triager.provider == "gemini":
                    result = P.ask_gemini(system, user, RESTANCE_TOOL)
                    time.sleep(P.GEMINI_SECONDS_BETWEEN_CALLS)
                else:
                    result = P.ask_claude(triager.client, system, user, RESTANCE_TOOL)
            except P.GeminiQuotaError as e:
                P.log(f"   AI quota reached ({e}), stance updates continue next run")
                triager.exhausted = True
                break
            except Exception as e:
                P.log(f"   AI error while re-judging stances: {str(e)[:200]}")
                continue
            if not result:
                continue
            version = result.pop("_model", None) or triager.model
            P.log_ai_call("restance", names[tid], version, phash, result)
            got = {s.get("n"): s.get("stance") for s in result.get("stances") or []}
            for i, (pid, finding, old) in enumerate(batch):
                new = got.get(i + 1)
                if new not in STANCES:
                    continue          # unanswered: stays pending, retried next run
                props = {"Claim used": P.p_text(claims[tid])}
                if new != old:
                    props["Stance"] = P.p_select(new)
                    props["Stance note"] = P.p_text(f"{P.TODAY}: was {old or 'empty'}, re-judged after the working claim changed")
                    changed += 1
                    rows.append([P.TODAY, names[tid], old or "", new, finding[:300]])
                P.update_page(pid, props)
    P.append_csv("stance_updates.csv", ["date", "theme", "old_stance", "new_stance", "finding"], rows)
    n = sum(len(v) for v in todo.values())
    if n or stamped:
        P.log(f"   working claims: {n} stances re-judged, {changed} changed" + (f", {stamped} of your findings stamped" if stamped else ""))
    return changed


# ----------------------------------------------------------------------------
# Notion helpers
# ----------------------------------------------------------------------------
def p_rel(ids):
    return {"relation": [{"id": i} for i in ids if i]}


def rel_ids(prop):
    return [r["id"] for r in (prop or {}).get("relation", [])]


def load_themes(P, ds):
    themes = []
    for page in P.query_all(ds):
        pr = page["properties"]
        name = P.read(pr.get("Theme")) or ""
        if name:
            themes.append({"id": page["id"], "name": name, "claim": P.read(pr.get("Working claim")) or ""})
    return themes


def trash(P, page_id):
    if P.DRY_RUN:
        return
    P.notion("PATCH", f"/pages/{page_id}", {"in_trash": True})


def _same(a, b):
    return _norm(a) == _norm(b)


# ----------------------------------------------------------------------------
# 1. Reviews: Accept -> Finding, Reject -> trash
# ----------------------------------------------------------------------------
def process_reviews(P, ids, themes, state):
    stats = state.setdefault("synthesis_stats", {"accepted_as_is": 0, "accepted_edited": 0, "rejected": 0,
                                                  "quotes_verified": 0, "quotes_unverified": 0,
                                                  "drafted": 0, "by_source": {}})
    by_name = {t["name"].lower(): t["id"] for t in themes}
    rows, pending = [], 0
    for page in P.query_all(ids["review"]):
        pr = page["properties"]
        decision = P.read(pr.get("Decision"))
        if decision not in ("Accept", "Reject"):
            pending += 1
            continue
        finding = P.read(pr.get("Finding")) or ""
        original = P.read(pr.get("AI original")) or ""
        quote = P.read(pr.get("Quote")) or ""
        papers = rel_ids(pr.get("Paper"))
        theme_ids = rel_ids(pr.get("Theme"))
        new_theme = (P.read(pr.get("New theme")) or "").strip()
        stance = P.read(pr.get("Stance"))
        source = P.read(pr.get("Source")) or ""
        edited = not _same(finding, original)
        if decision == "Accept":
            if not theme_ids and new_theme:
                tid = by_name.get(new_theme.lower())
                if not tid:
                    tid = P.create_page(ids["themes"], {
                        "Theme": P.p_title(new_theme), "Origin": P.p_select("Proposed by AI"),
                        "Chapter": P.p_select("Unplaced"), "Kind": P.p_select("Theme")})
                    if tid:
                        by_name[new_theme.lower()] = tid
                        themes.append({"id": tid, "name": new_theme, "claim": ""})
                theme_ids = [tid] if tid else []
            if quote.startswith(UNVERIFIED):
                quote = ""      # an unverified quote never reaches the accepted findings
            fid = P.create_page(ids["findings"], {
                "Finding": P.p_title(finding), "Paper": p_rel(papers), "Theme": p_rel(theme_ids),
                "Stance": P.p_select(stance if stance in STANCES else None),
                "Locator": P.p_text(P.read(pr.get("Locator")) or ""), "Quote": P.p_text(quote),
                "Origin": P.p_select("AI draft (edited)" if edited else "AI draft (as is)"),
                "Claim used": P.p_text(P.read(pr.get("Claim used")) or ""),
                "Stance note": P.p_text(P.read(pr.get("Stance note")) or ""),
            })
            if fid is None and not P.DRY_RUN:
                continue
            stats["accepted_edited" if edited else "accepted_as_is"] += 1
        else:
            stats["rejected"] += 1
        trash(P, page["id"])
        rows.append([P.TODAY, decision, "edited" if (decision == "Accept" and edited) else "",
                     source, stance or "", papers[0] if papers else "", original[:300]])
    P.append_csv("findings_review.csv",
                 ["date", "decision", "edited", "source", "stance", "paper_page", "ai_original"], rows)
    if rows:
        P.log(f"   reviews processed: {sum(r[1] == 'Accept' for r in rows)} accepted, "
              f"{sum(r[1] == 'Reject' for r in rows)} rejected")
    return pending


# ----------------------------------------------------------------------------
# 2. Drafts for papers you have read
# ----------------------------------------------------------------------------
def draft_findings(P, papers, triager, themes, ids, state, cfg):
    if not triager.available:
        return 0
    done_before = set(state.setdefault("drafted", []))
    todo = [p for p in papers
            if p["status"] == "Read" and not p.get("findings_count") and not p.get("drafts_count")
            and p["page_id"] not in done_before and p.get("tier") != "Discard"]
    todo.sort(key=lambda p: -(p.get("relevance") or 0))
    by_name = {t["name"].lower(): t["id"] for t in themes}
    claim_of = {t["id"]: t["claim"] for t in themes}
    stats = state["synthesis_stats"]
    made = 0
    drafter = Findings(P, triager, themes, cfg)
    for p in todo[:cfg["max_papers_per_run"]]:
        text, source = get_text(p, cfg["max_chars"])
        if not text.strip():
            text, source = p.get("title", ""), SOURCE_ABSTRACT
        found = drafter.draft(p, text, source)
        if triager.exhausted:
            break
        if found is None:
            continue
        for f in found[:cfg["max_findings_per_paper"]]:
            claim = (f.get("finding") or "").strip()
            if not claim:
                continue
            quote = (f.get("quote") or "").strip()
            ok, page = locate_quote(quote, text) if quote else (False, None)
            locator = (f.get("locator") or "").strip()
            if page:
                locator = f"p. {page}" + (f", {locator}" if locator else "")
            if quote:
                stats["quotes_verified" if ok else "quotes_unverified"] += 1
                if not ok:
                    quote = UNVERIFIED + quote
            tid = by_name.get((f.get("theme") or "").strip().lower())
            P.create_page(ids["review"], {
                "Finding": P.p_title(claim), "AI original": P.p_text(claim),
                "Paper": p_rel([p["page_id"]]), "Theme": p_rel([tid] if tid else []),
                "New theme": P.p_text("" if tid else (f.get("new_theme") or f.get("theme") or "")),
                "Stance": P.p_select(f.get("stance") if f.get("stance") in STANCES else "Describes"),
                "Locator": P.p_text(locator), "Quote": P.p_text(quote),
                "Source": P.p_select(source), "Decision": P.p_select("To review"), "Drafted": P.p_date(P.TODAY),
                "Claim used": P.p_text(claim_of.get(tid, "")),
            })
            made += 1
        stats["drafted"] += 1
        stats["by_source"][source] = stats["by_source"].get(source, 0) + 1
        done_before.add(p["page_id"])
        P.log(f"   drafted {len(found)} findings for {p['name'][:70]} ({source})")
    state["drafted"] = sorted(done_before)
    if todo:
        P.log(f"   {made} draft findings added to the Review queue"
              + (f", {len(todo) - min(len(todo), cfg['max_papers_per_run'])} read papers left for the next runs"
                 if len(todo) > cfg["max_papers_per_run"] else ""))
    return made


# ----------------------------------------------------------------------------
# 3. Tiers (only where empty, never overwritten)
# ----------------------------------------------------------------------------
def suggest_tiers(P, papers):
    lib = {P.short_id(p["work"]["id"]): p for p in papers if p.get("work")}
    cited_by = {k: 0 for k in lib}
    for p in papers:
        for ref in (p.get("work") or {}).get("referenced_works") or []:
            k = P.short_id(ref)
            if k in cited_by:
                cited_by[k] += 1
    n = 0
    for p in papers:
        if p.get("tier") or p.get("relevance") is None:
            continue
        rel = p["relevance"]
        k = P.short_id(p["work"]["id"]) if p.get("work") else None
        central = (k and cited_by.get(k, 0) >= 2) or (p.get("percentile") or 0) >= 90
        tier = "Core" if rel >= 5 or (rel >= 4 and central) else ("Supporting" if rel >= 3 else "Background")
        P.update_page(p["page_id"], {"Tier": P.p_select(tier)})
        p["tier"] = tier
        n += 1
    if n:
        P.log(f"   proposed a Tier for {n} papers (yours to change, never overwritten)")
    return n


# ----------------------------------------------------------------------------
def run(P, papers, triager, state, config):
    n = config.get("notion") or {}
    ids = {"themes": n.get("themes_data_source"), "findings": n.get("findings_data_source"),
           "review": n.get("review_queue_data_source")}
    cfg = settings(config)
    out = {"drafts_added": 0, "pending_review": 0, "tiers": 0}
    if cfg["suggest_tiers"]:
        try:
            out["tiers"] = suggest_tiers(P, papers)
        except RuntimeError as e:
            P.log("   tiers skipped (is there a Tier select property in Notero?): " + str(e)[:150])
    if not all(ids.values()):
        P.log("   synthesis matrix not configured (themes/findings/review queue IDs missing in config.json)")
        return out
    try:
        themes = load_themes(P, ids["themes"])
        out["pending_review"] = process_reviews(P, ids, themes, state)
        out["restanced"] = restance(P, triager, themes, ids, state)
        out["drafts_added"] = draft_findings(P, papers, triager, themes, ids, state, cfg)
        out["pending_review"] += out["drafts_added"]
    except RuntimeError as e:
        P.log("   synthesis step stopped (connect your integration to the Synthesis matrix databases): "
              + str(e)[:200])
    if not ZOTERO_KEY:
        P.log("   tip: add ZOTERO_API_KEY and ZOTERO_USER_ID secrets so drafts use your full-text PDFs")
    out["stats"] = state.get("synthesis_stats", {})
    return out
