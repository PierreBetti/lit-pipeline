# lit-pipeline

Every night, this runs a living literature review on top of your Notero database in Notion:

1. **Citation metrics** from OpenAlex (citations, field-normalized percentile, FWCI) and **retraction alerts**.
2. **AI triage** of each abstract: summary, relevance (1 to 5) and why, research questions, ecosystem,
   gases, methods, key result. Notion turns it into a **Priority** score.
3. **Structured extraction**: study sites (geocoded for free with OpenStreetMap), biome, study design,
   duration and reported values.
4. **Suggested papers**, Connected Papers-style: Similarity (shared references + co-citation, weighted by
   your own relevance ratings), Role (Prior / Derivative / Similar work), AI relevance, and a **Global
   score**. Once you have screened enough suggestions, a **Personal score** learned from your own
   Added / Not relevant decisions.
5. **Dashboard** (GitHub Pages, embed it in Notion) with five tabs: citation map, study site map,
   evidence gap map, timeline (your library vs. the whole field, milestones, coverage check), and
   screening (PRISMA 2020-style flow, exclusion reasons, saturation curve, learned preferences).
6. **Audit trail**: one row per run in the Notion *Review log*, plus `logs/review_log.csv`,
   `logs/decisions.csv` (every screening decision, dated) and `data/state.json`, all committed to the
   repository every night, so the whole review is timestamped and reproducible.

**Everything is free**: GitHub Actions runs it, OpenAlex and Semantic Scholar are free, and the AI step
uses the free tier of Google's Gemini API. If the free daily quota runs out, the remaining papers are
simply done the next night.

---

## One-time setup (about 20 minutes)

### 1. Create the GitHub repository
1. Create a free account at https://github.com (with your UQAM address you can also claim the
   GitHub Student Developer Pack).
2. New repository, name it `lit-pipeline`. **Private works**: Actions are free on private repos
   (this uses a few minutes a day out of 2,000 free per month). Publishing the map with GitHub Pages
   from a private repo needs GitHub Pro, which the Student Developer Pack gives you for free. Either
   way, the published map page itself is reachable by anyone who has its address.
3. Upload every file of this folder, keeping the structure. The `.github/workflows/nightly.yml` file is
   in a hidden folder: if the web upload skips it, use **Add file > Create new file**, type
   `.github/workflows/nightly.yml` as the name and paste its content.

### 2. Create the Notion integration
1. Go to https://www.notion.so/profile/integrations > **New integration** (type: Internal).
   Name it `lit-pipeline`. Copy the **Internal Integration Secret**.
2. Give it access to both databases: open **📚 Notero**, click `•••` > **Connections** > add
   `lit-pipeline`. Do the same on the **Literature Matrix** page (this covers 🧭 Suggested papers,
   which lives inside it).

### 3. Get the API keys
- **OpenAlex** (free): create an account at https://openalex.org, then copy your key from
  https://openalex.org/settings/api
- **Gemini API** (free): https://aistudio.google.com > **Get API key**, sign in with a Google
  account, create a key. Do **not** enable billing on it, so it can never charge you. Note that on the
  free tier Google may use what you send to improve its models: here that is only published abstracts
  and your `research_context.md`.
- **Semantic Scholar** (optional): works without a key; you can request one at
  https://www.semanticscholar.org/product/api if you hit rate limits.

### 4. Add the secrets to GitHub
Repository > **Settings > Secrets and variables > Actions > New repository secret**:

| Name | Value |
|---|---|
| `NOTION_TOKEN` | the Notion integration secret |
| `OPENALEX_API_KEY` | your OpenAlex key |
| `GEMINI_API_KEY` | your Gemini key |
| `CONTACT_EMAIL` | your email (polite pool for OpenAlex) |
| `S2_API_KEY` | optional |

### 5. First run
Repository > **Actions** > **Literature pipeline** > **Run workflow**. Watch the log: it lists how many
papers were matched, triaged and suggested. After that it runs by itself every night.

### 6. Publish and embed the map
1. Repository > **Settings > Pages** > Source: **Deploy from a branch**, branch `main`, folder `/docs`.
2. After a minute your map is at `https://<your-username>.github.io/lit-pipeline/`.
3. In Notion, on the Literature Matrix page, type `/embed` and paste that address.

---

## Day-to-day

- **Re-run the AI on a paper**: clear its *Triage date* in Notion.
- **Change how Priority is weighted**: edit the *Priority* formula in the Notero database.
- **Change how suggestions are ranked**: edit the *Global score* formula in Suggested papers, and the
  `global_score` function in `pipeline.py` (it uses the same weights to choose which candidates to keep).
- **Your project evolves**: edit `research_context.md`, then clear *Triage date* on papers you want re-scored.
- **A suggestion looks good**: add it to Zotero (ResearchRabbit or the DOI). Next night it is marked
  *Added to Zotero* automatically. Mark the others *Not relevant* so they leave the list.
- **Hitting the free quota often**: lower `MAX_TRIAGE_PER_RUN` (default 40) or raise
  `GEMINI_SECONDS_BETWEEN_CALLS` (default 7) in `nightly.yml`. A big first import just takes a few nights.
- **Paid alternative**: the script also supports the Claude API (`ANTHROPIC_API_KEY`), but you do not need it.
- **Screening a suggestion**: set *Exclusion reason* first, then *Decision* to Not relevant (the row leaves
  the To review view once the decision changes). The reason feeds the PRISMA flow.
- **Re-extract sites and data for a paper**: clear its *Extraction date*.
- **The field trend**: edit `FIELD_QUERY` at the top of `pipeline.py` to describe your field (OpenAlex
  boolean search on titles and abstracts).
- **Test without writing anything**: `python pipeline.py --dry-run` (or `--no-ai` to skip the AI step).

Good to know: the AI only sees titles and abstracts, so treat its relevance score as triage, not a
verdict. The map page is public on GitHub Pages; it only contains titles, DOIs and categories
(no AI notes), and the Notion links on it only open for you.
