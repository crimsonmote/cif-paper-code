# From Science to Industry: code and data

Code and venue-level data for *From Science to Industry: Quantifying the
Commercialization Influence of Scholarly Research* (X. K. Gao, K. Ramachandran,
R. Sivakumar).

The Commercialization Influence Factor (CIF) scores every paper in the OpenAlex
citation network by Personalized ArticleRank, with restarts at the papers cited
by patents of industry assignees (the seed set). A venue's CIF is the sum of its
works' scores; normalized CIF divides that sum by the venue's number of works.

The repository has three parts:

| Path | Contents |
| --- | --- |
| `data_s1/` | Data S1: venue-level CIF and c_n counts for all 243,257 OpenAlex sources (see `data_s1/README.md`) |
| `common/`, `ref_map/` | Pipeline: OpenAlex ingest, citation graph, seed sets, reachability, ArticleRank, venue aggregation |
| `analysis/` | Scripts that produce the tables, figures and numbers reported in the paper and the Supplementary Materials |

Most analyses need only Data S1 and the venue table built from it
(see [Reproducing the analyses](#reproducing-the-analyses)). Recomputing CIF itself
requires the full pipeline.

## Requirements

- Python 3.10 or later with the packages in `pyproject.toml`
  (`uv sync`, or `pip install` them into a virtual environment).
- R with `quantreg` for the regression scripts (`analysis/*.R`).
- For the pipeline only: MongoDB, and Neo4j with the Graph Data Science (GDS)
  library. APOC is used for batched writes when available; pass `--no-apoc` to the
  scripts that offer it otherwise. The full network has about 272 million works.

No versions are pinned. The results were produced with Python 3.12 (numpy 2.3,
pandas 2.3, scipy 1.16, matplotlib 3.10, pymongo 4.15, neo4j driver 6.0) and R 4.6
with quantreg 6.1.

Run every command from the repository root.

## Configuration

Credentials are read from environment variables, or from the OS keyring when a
variable is unset. `.env.example` lists the variables:

| Variable | Keyring entry (service, key) | Used for |
| --- | --- | --- |
| `MONGO_URI` | `mongo`, `uri` | MongoDB holding the OpenAlex snapshot (database `openalex`) |
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASS` | `neo4j`, `uri` / `user` / `pass` | Neo4j citation graph |
| `NEO4J_DATABASE` | | Neo4j database name (default `neo4j`) |
| `SC_DATA_DIR` | | Directory of raw inputs (default `<repo>/data`) |
| `OPENALEX_SNAPSHOT_DIR` | | Extracted OpenAlex snapshot (default `$SC_DATA_DIR/openalex`) |

No code reads `.env` automatically. Export the variables in your shell, for example
with `set -a; . ./.env; set +a`.

## Data

### Data S1 (included)

Seven xz-compressed JSON files, one record per OpenAlex source, joined on
`source_id`. Field definitions are in `data_s1/README.md`. Records are in the order
used for the paper's analyses, which decides ties at top-N cutoffs; the venue table
and `build_data_s1` keep that order. Data S1 is licensed
CC BY-NC 4.0 (`data_s1/LICENSE`), because it is derived in part from the Reliance on
Science data, which carry that licence.

### Raw inputs (not included)

Place these under `$SC_DATA_DIR`:

```text
$SC_DATA_DIR/
├── openalex/                         OpenAlex snapshot of 21 August 2025, extracted
│                                     (one subdirectory per entity, plus merged_ids/)
├── reliance_on_science/
│   ├── _patent_paper_pairs.csv       Reliance on Science, release of 3 June 2024
│   └── _pcs_oa.csv                   doi:10.5281/zenodo.11461587 (CC BY-NC 4.0)
├── founding_patents/
│   ├── ocpb_assigneeatfiling.csv     Founding Patents, release of 10 July 2024
│   │                                 doi:10.5281/zenodo.12706672 (CC BY 4.0)
│   ├── _patent_ocpb.csv              Founding Patents, release of 2 December 2023
│   │                                 doi:10.5281/zenodo.10250493 (CC BY 4.0)
│   └── _patent_paper_startup_triplets.csv   built by step 1 below
├── clarivate/
│   └── jcr_journal_results_2025_comprehensive.csv
└── scimago/
    └── scimagojr_2025.csv
```

- **OpenAlex** snapshots are distributed at <https://docs.openalex.org/download-all-data/openalex-snapshot> (CC0).
- **Journal Impact Factor.** Journal Citation Reports is licensed from Clarivate and
  cannot be redistributed. Export the 2025 release (2024 JIF) from JCR with the
  ISSN, eISSN and JIF columns. `ref_map/aggregation/build_jcr_reference_from_html.py`
  can build the same CSV from saved JCR journal pages.
- **SJR.** Download the 2025 journal rankings (2024 SJR) from
  <https://www.scimagojr.com/journalrank.php> (the "Download data" link).

## Pipeline

The steps below rebuild CIF from the raw inputs. Each `python -m` module prints its
options with `--help`. Outputs go to `ref_map/aggregation/output/`,
`ref_map/output/` and `ref_map/reachability/output/`.

1. **Seed linkage.** Join Founding Patents assignees with the Patent-Paper Pairs:

   ```bash
   python -m ref_map.export.build_seed_linkage \
       --assignees $SC_DATA_DIR/founding_patents/ocpb_assigneeatfiling.csv \
       --pairs $SC_DATA_DIR/reliance_on_science/_patent_paper_pairs.csv \
       --out $SC_DATA_DIR/founding_patents/_patent_paper_startup_triplets.csv
   ```

2. **Ingest OpenAlex into MongoDB.** Loads the snapshot, flags works linked to
   patents and to the seed set, removes merged entities, and builds indexes:

   ```bash
   python -m common.ingest_openalex          # --steps ingest,merged,indexes
   ```

3. **Build the Neo4j citation graph** from MongoDB, then write the seed-definition
   properties:

   ```bash
   python -m ref_map.export.neo4j_export
   python -m ref_map.export.founding_patent_seed_patch
   ```

4. **Reachability.** Minimum citation hops from the seed set for every work, and the
   hop histogram (fig. S5):

   ```bash
   python -m ref_map.reachability.node_reachability --mode cypher-full --seed-mode legacy-startup --workers 8
   python -m ref_map.reachability.hop_histogram
   ```

5. **CIF.** Personalized ArticleRank on the reachable subgraph for the four seed
   definitions at d = 0.85. The `full_par` runs write the same seed set's scores
   at the GDS default damping (0.85; `startup_par`, read by the per-field and
   top-paper scripts) and at d = 0.95 (SM S4.7):

   ```bash
   python -m ref_map.centrality.founding_patent_cif_suite
   python -m ref_map.centrality.full_par --mode full --seed-mode legacy-startup
   python -m ref_map.centrality.full_par --mode full --seed-mode legacy-startup --damping-factor 0.95
   ```

6. **Venue and year aggregation:**

   ```bash
   python -m ref_map.aggregation.matt_marx_source_aggregation --resume
   python -m ref_map.aggregation.source_aggregation --target reachability --mode full \
       --seed-mode legacy-startup --depth-thresholds 0,1,5,10
   python -m ref_map.aggregation.source_aggregation --target par --mode full \
       --seed-mode legacy-startup --par-prop startup_par_0.95
   python -m ref_map.aggregation.year_aggregation --target reachability --mode full \
       --seed-mode legacy-startup
   python -m ref_map.aggregation.venue_controls       # year and field controls per venue
   ```

7. **Seed-specificity test (SM S4.6).** Three stratum-matched seed samples, one
   shared subgraph, four ArticleRank runs, venue aggregation:

   ```bash
   python analysis/build_paper_absorption_hist.py
   python -m ref_map.export.build_m5_placebo_seeds
   python -m ref_map.export.m5_placebo_seed_patch
   python -m ref_map.reachability.m5_union_reachability --workers 8
   python -m ref_map.centrality.m5_placebo_par_suite --resume
   python -m ref_map.aggregation.m5_source_aggregation --resume
   ```

8. **Data S1 and the venue table:**

   ```bash
   python -m ref_map.aggregation.build_data_s1          # writes data_s1/
   python -m ref_map.aggregation.build_venue_basis      # writes ref_map/output/venue_basis.csv
   ```

`build_venue_basis` reads Data S1, adds the OpenAlex h-index and ISSNs from MongoDB,
and matches JIF and SJR by ISSN.

### Naming

Some identifiers predate the paper's terminology and are kept because they name
properties in the database:

- `startup` (as in `startup_par`, `startup_min_hops`, seed mode `legacy-startup`)
  is the industry-absorbed seed set: all papers cited by patents in the
  Founding Patents assignee linkage. It is the same set as the `all-assignees`
  definition. It does not mean young firms, which is the `young-firm` definition.
- `matt_marx_*` properties hold the Reliance on Science and Founding Patents links,
  after the datasets' author.
- `m5` and `placebo` refer to the seed-specificity test of SM S4.6 and its matched
  seed samples.

## Reproducing the analyses

The analysis scripts read `ref_map/output/venue_basis.csv` and `data_s1/`. Build the
venue table with step 8 (the Data S1 files are already in the repository; the
venue table needs MongoDB for the h-index and ISSNs, and the JCR and SJR files).
Outputs go to `analysis/figs/`, `analysis/results/` and
`analysis/regression_output/`.

| Paper item | Script |
| --- | --- |
| Fig. 1 | `analysis/scatter_figure.py` |
| Table 1A, SM S3.5 correlations | `analysis/correlations.py`, `analysis/emit_correlations.py`, `analysis/panelA_hindex.py` |
| Table 1B, SM S4.2 deviance and quantile regressions | `analysis/build_venue_analysis_table.py`, then `analysis/venue_regression.R`, `analysis/panelB_bootstrap.py`, `analysis/panelB_qr_bootstrap.R`, `analysis/panelB_hindex.py`, `analysis/panelB_qr_hindex.R` |
| SM S3.1–S3.2 top papers and venues | `analysis/top_cif_papers.py`, `analysis/rankings.py` |
| SM fig. S1, seed field composition | `analysis/seed_field_composition.py`, `analysis/seed_domain_figure.py` |
| SM S3.3 top papers and venues by field | `analysis/top_cif_per_field.py`, `analysis/top_venues_per_field.py`, `analysis/venues_norm_per_field.py`, `analysis/emit_field_tables.py` |
| SM S3.4 overperformers | `analysis/emit_overperformers.py` |
| SM S4.1 within-field correlations | `analysis/within_field_correlations.py` |
| SM S4.3 h-index comparisons | `analysis/hindex_comprehensive.py` |
| SM S4.4 paper-level absorption | `analysis/build_paper_absorption_hist.py`, `analysis/paper_absorption_regression.R`, `analysis/paper_absorption_figure.py` |
| SM S4.5 measurement robustness | `analysis/articles_basis_deviance.py`, `analysis/floored_deviance.py` |
| SM S4.6 seed-specificity test | `analysis/m5_placebo_analysis.py` |
| SM S4.7 damping factor | `analysis/damping_095_analysis.py` |
| SM S5 seed definitions | `analysis/seed_universe_analysis.py`, `analysis/emit_seed_scope_topvenues.py` |
| SM S6 reachability and c_n | `analysis/min_hop_figure.py`, `analysis/year_reachability_figure.py`, `analysis/emit_cn_tables.py` |

The scripts differ in what they need:

- **Data S1 and the venue table:** `scatter_figure.py`, `correlations.py`,
  `emit_correlations.py`, `panelA_hindex.py`, `rankings.py`,
  `emit_overperformers.py`, `hindex_comprehensive.py`, `within_field_correlations.py`,
  `damping_095_analysis.py`, `seed_universe_analysis.py`,
  `emit_seed_scope_topvenues.py`, `emit_cn_tables.py`, and
  `build_venue_analysis_table.py`. The last builds
  `analysis/regression_output/venue_analysis_table.csv`, which the deviance and
  quantile-regression scripts (`venue_regression.R`, `panelB_*`,
  `articles_basis_deviance.py`, `floored_deviance.py`) and `m5_placebo_analysis.py`
  read.
- **The pipeline databases or outputs:** the per-field and top-paper lists
  (`top_cif_papers.py`, `top_cif_per_field.py`, `top_venues_per_field.py`,
  `venues_norm_per_field.py`, then `emit_field_tables.py`), `seed_field_composition.py`
  (then `seed_domain_figure.py`), `build_paper_absorption_hist.py` (then
  `paper_absorption_regression.R` and `paper_absorption_figure.py`), and the two
  reachability figures, which read the outputs of pipeline steps 4 and 6.

## Licence

Code: MIT (`LICENSE`). Data S1: CC BY-NC 4.0 (`data_s1/LICENSE`).
