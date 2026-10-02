# field_corpus.csv: data dictionary

One row per work retrieved for the field-wide systematic map. Regenerated every night. Abstracts are not included.

| Column | Description |
|---|---|
| openalex_id | OpenAlex work ID (W...). Resolve at https://openalex.org/<id> |
| doi | DOI URL, when known |
| title | Title as given by OpenAlex |
| year | Publication year |
| cited_by | Citation count in OpenAlex when retrieved |
| scopes | Scopes (from scopes.json) the work belongs to, separated by `|` |
| on_topic | 1 if the AI judged the work to study greenhouse gas or carbon exchange in wetlands, 0 if not, empty if not classified yet |
| research_questions | Research questions the work informs, `|`-separated |
| ecosystems | Ecosystem types, `|`-separated |
| gases | Gases studied, `|`-separated |
| biome | Biome |
| study_design | Study design |
| sites | Named study sites as "name, region, country", `|`-separated |
| geocoded_points | Coordinates "lat,lon" of the sites that could be geocoded, `|`-separated |
| title_only | 1 if no abstract was available and the work was classified from its title only |
| model_version | Exact AI model version that produced the classification |
| prompt_version | Hash of the prompt and schema used (full text archived in data/state.json) |
| classified_on | Date of classification |

All classifications are produced by a language model and validated on a hand-coded sample (see METHODS.md and the dashboard's Validation tab).
