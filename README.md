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
6. **Field-wide systematic map** (`scopes.json`): three nested searches of OpenAlex, from the most
   specific (forested swamps) to the broadest (all wetlands). Every paper is classified once by the AI
   (on-topic or not, research questions, ecosystem, gases, biome, design, sites), innermost scope first,
   a batch per night. Scopes too large to classify fully are represented by a fixed random sample, and
   their numbers become estimates with 95% confidence intervals. This powers the **Field rings** tab and
   the field/compare modes of the gap map, site map and timeline.
7. **Audit trail**: one row per run in the Notion *Review log*, plus `logs/review_log.csv`,
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
| `ZOTERO_API_KEY` | optional: lets AI-drafted findings use your PDFs (zotero.org/settings/keys, read-only access to your library) |
| `ZOTERO_USER_ID` | optional: the number shown as "Your user ID for use in API calls" on that same page |

### 5. First run
Repository > **Actions** > **Literature pipeline** > **Run workflow**. Watch the log: it lists how many
papers were matched, triaged and suggested. After that it runs by itself every night.

### 6. Publish and embed the map
1. Repository > **Settings > Pages** > Source: **Deploy from a branch**, branch `main`, folder `/docs`.
2. After a minute your map is at `https://<your-username>.github.io/lit-pipeline/`.
3. In Notion, on the Literature Matrix page, type `/embed` and paste that address.

---

## Semantic layer and digest

- A small open-source model runs inside the workflow (no AI quota): every suggestion gets a **Semantic match**
  with your research questions, a **semantic discovery** channel finds papers by meaning, and the **Personal
  score** learns from the content of what you keep and exclude. The first run downloads the model (about
  130 MB, then cached), so it takes a few minutes longer.
- Edit the full wording of your research questions in `config.json` (`research_question_descriptions`):
  that is what the model compares papers against.
- The Screening tab estimates **how many relevant papers are left** in your queue.
- A **📰 Weekly digest** page appears in Notion every 7 days.
- The field map now includes French search terms, theses, reports, chapters and preprints (`scopes.json`).

## Lenses and geography

The dashboard has a control bar (on the Study sites, Evidence gaps, Field rings and Timeline tabs):
**Show** (my library / library vs field / field only), **Field** (which scope) and **Where** (World, Canada,
Québec by default, editable in `config.json` under `geography`). The choice is kept in the link, so a Notion
embed can show a fixed view, for example `…/lit-pipeline/#gaps?lens=field&scope=forested&geo=quebec`.
The Evidence gaps tab also has a table following each research question from the world level down to the
most local one. Below the world level, numbers rely on papers whose study sites could be located.

## Synthesis matrix (findings, themes, gaps)

In Notion, under Literature Matrix > **📖 Synthesis matrix**. The unit is a *finding*: one claim, from one
paper, tied to one theme, with a stance (Supports / Counters / Mixed / Describes) relative to that theme's
**working claim**. Each theme then gets a colored verdict: GAP (no finding), THIN (one or two), CONTESTED
(findings on both sides), COVERED (three or more, consistent).

- **What runs by itself.** When you mark a paper **Read**, the next night drafts up to 8 findings for it
  (3 papers per night by default, `config.json` > `synthesis`). The text comes from your Zotero PDF, else an
  open-access PDF, else the abstract (shown in *Source*). Each draft carries a short quote; the quote is checked
  word for word against the text and its page number is added to *Locator*. A quote that cannot be found is
  flagged ⚠ and is never copied to an accepted finding. Empty **Tier** fields are filled with a proposal
  (Core / Supporting / Background, from relevance and how central the paper is); yours are never overwritten.
- **What stays yours.** Drafts wait in **✅ Review queue**. Set *Decision* to Accept (edit the text first if
  needed) or Reject; the next run turns accepted drafts into Findings and removes the queue rows. Only accepted
  findings count in the verdicts. You also own the themes and their working claims, which drive the stances.
- **Editing a working claim is safe.** Each finding remembers the claim its stance was judged against
  (*Claim used*). When you change a claim, or move a finding to another theme, the next run re-judges those
  stances from the finding text and explains any change in *Stance note* (`logs/stance_updates.csv`).
  Findings you wrote yourself are never re-judged the first time: your stance is taken as given.
- **Measured.** Accept-as-is / edited / rejected rates and the share of verified quotes appear in the
  Validation tab (decisions in `logs/findings_review.csv`).
- Full texts are read in memory only and never saved to the repository.

## Validation (numbers for a methods paper)

Run from **Actions > Validation > Run workflow**, choosing a task. Results appear in the dashboard's
**Validation** tab (`#valid`). Full protocol in `METHODS.md`.

1. **calibration** (no setup): checks that the confidence intervals of sampled scopes are honest. Needs one
   fully classified scope of 150+ papers.
2. **consistency** (no setup): re-classifies 30 papers to measure the AI's test-retest agreement.
3. **sample**, then hand-code, then **evaluate**: draws a blind random sample (50 per scope, 30 double-coded)
   into the 🧪 Validation sample database in Notion. Code each row from its title and abstract without
   looking anything up, tick *Coded*, then run *evaluate* for AI vs human (and human vs human) agreement.
   Rows marked Coder B are best coded by a second person.
4. **preferences**: cross-validated and prospective accuracy of the Personal score (needs screening decisions).
5. **benchmark**: copy `benchmarks/_TEMPLATE.json`, paste the DOIs of the included studies of a published
   systematic review, run *benchmark*. Optionally record what Connected Papers / ResearchRabbit / Litmaps
   found with the same seeds under `comparators`.

`all` runs everything that has the data it needs. Reproducibility settings are in `config.json`
(temperature, model pinning); every AI answer is logged in `data/ai_log.jsonl`.

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
- **Refine the field**: edit the queries in `scopes.json` (OpenAlex boolean search on titles and
  abstracts; no commas inside a query). Only papers new to the corpus get classified, so refining is cheap.
  Keep the scopes ordered from most specific to broadest.
- **Reading the gap comparison**: a *research gap* means the field itself has almost nothing there; a
  *reading gap* means the field has papers but your library has none of them.
- **Field map speed**: `max_classify_per_run` in `scopes.json` (default 200 per night, within the free
  Gemini quota). The field data lives in `data/corpus.json`, not in Notion.
- **Test without writing anything**: `python pipeline.py --dry-run` (or `--no-ai` to skip the AI step).

Good to know: the AI only sees titles and abstracts, so treat its relevance score as triage, not a
verdict. The map page is public on GitHub Pages; it only contains titles, DOIs and categories
(no AI notes), and the Notion links on it only open for you.
