# Data S1

Venue-level outputs for every OpenAlex source (journals, conferences, and all other
source types; 243,257 records per file). All files join on `source_id`. Each file
holds only the quantity it is named for.

CIF is the Commercialization Influence Factor: paper-level Personalized ArticleRank
scores (damping d = 0.85 unless stated) summed over the works whose OpenAlex primary
source is the venue. Normalized CIF is `total_score / total_papers`, computed from
`cif_all_documents.json`.

Each file is xz-compressed JSON (`.json.xz`): a list of records. Read it with
`lzma.open(path, "rt")` in Python or `jsonlite::fromJSON(xzfile(path))` in R.

| File | Fields |
|---|---|
| `cif_all_documents.json` | `source_id`, `source_name`, `source_type` (`j` journal, `c` conference, other OpenAlex types), `total_papers` (works with the source as primary source), `scored_papers` (works with a nonzero score), `total_score` (summed CIF) |
| `cif_articles.json` | the same fields, counting only works typed `article`; sources with no articles are omitted |
| `cn_counts.json` | `source_id`, `c_0`, `c_1`, `c_5`, `c_inf`: number of the source's works within 0, 1, 5, or any number of citation hops upstream of an industry-absorbed paper (all document types; cumulative in the hop count) |
| `cif_seed_definitions.json` | `source_id`, summed CIF with the seed set restricted to each assignee-class definition: `corporate`, `young_firm`, `young_firm_university`. The all-assignee definition is the base file |
| `cif_matched_samples.json` | `source_id`, summed CIF from the four runs of the seed-specificity test, computed on one shared subgraph: `cif_union_graph` (the industry-absorbed seed set) and `sample_1`–`sample_3` (stratum-matched seed samples) |
| `cif_damping_095.json` | `source_id`, `total_score`: summed CIF at damping d = 0.95 |
| `venue_controls.json` | `source_id`; publication-year statistics of the source's works: `n_yr` (works with a year), `first_year`, `p05_year` (5th percentile), `mean_year`, `median_year`, `last_year`; and `field_counts`, the source's OpenAlex topic counts summed by parent field (keys are OpenAlex field numbers, `https://openalex.org/fields/<n>`; empty when the source lists no topics). These are the field and vintage controls of the analysis of deviance |

A source with no reachable works has zero in every score and count field.
Fifteen sources have no display name in OpenAlex; their `source_name` is empty.

Records are listed in the same source order in every file: the order used for the
paper's analyses. Top-N selections in the analysis scripts break ties on the ranking
metric by this order, so keep it to reproduce the reported numbers exactly.
