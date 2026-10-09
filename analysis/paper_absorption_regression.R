#!/usr/bin/env Rscript
# Stage 2 of the paper-level absorption analysis (c_0 / seed-set regression).
#
# Reads regression_output/paper_absorption_hist.csv.gz (built by
# build_paper_absorption_hist.py: one row per (field, year, cited_by_count)
# with paper count n and per-flag absorbed counts k_*) and fits, for each
# outcome flag, the grouped binomial GLM
#
#     glm( cbind(k, n-k) ~ factor(field) + factor(year) + cite_bin,
#          family = binomial )
#
# where cite_bin is the paper's within-(field x year) citation PERCENTILE bin
# (midrank convention for the heavy ties at 0/1 citations). Because all
# predictors are discrete, this grouped fit has exactly the individual-level
# Bernoulli likelihood over ~200M papers.
#
# Layers (mirroring venue_regression.R):
#   (1) sequential analysis of deviance (field -> cohort -> citations),
#       LR chi-square tests  [ANOVA equivalent; raw deviance units]
#   (2) exact 3-block Shapley decomposition of D^2 = McFadden pseudo-R^2
#       (Bernoulli log-likelihood, so grouped == individual-level exactly)
#       [ANCOVA R^2-decomposition equivalent; shares sum to full D^2]
#   (3) plain-reader layer: absorption rate by citation bin (dose-response),
#       top-bin/bottom-bin rate ratio, citations-only AUC (within-stratum
#       percentile as score), full-model AUC, Tjur R^2
#   (4) robustness one-liners: complementary log-log link; linear
#       probability model (weighted lm on k/n)
#
# Outcome flags: main = paper_patent_startup (190,224 papers; matches the
# manuscript's ~190k industry-absorbed seed), plus paper_patent_v2 (full
# Patent-Paper Pairs, 335,795) and cited_by_patent (any patent NPL citation,
# 7.05M) as construct-robustness variants.
#
# Caveats carried in the output: PU labels (matching incompleteness lowers
# absolute rates; shape/AUC preserved under selected-at-random), cohort
# censoring handled by COHORT range, discrete-tie binning (a tied citation
# value is assigned wholly to its midrank bin, so bin sizes are approximate).
#
# Outputs (regression_output/): paper_absorption_results.md,
#   paper_absorption_shapley.csv, paper_absorption_rates.csv,
#   paper_absorption_seq_deviance.txt

HERE <- tryCatch(dirname(sub("^--file=", "",
          grep("^--file=", commandArgs(FALSE), value = TRUE))),
          error = function(e) ".")
if (length(HERE) == 0 || HERE == "") HERE <- "."
OUT <- file.path(HERE, "regression_output")
HIST <- file.path(OUT, "paper_absorption_hist.csv.gz")

# ---- config ----
FLAGS <- c(main_startup = "k_paper_patent_startup",
           ppp_v2       = "k_paper_patent_v2",
           any_patent   = "k_cited_by_patent")
COHORT_MIN <- 1950
COHORT_MAX <- 2015          # absorption lag: exclude under-exposed cohorts
MIN_STRATUM <- 100          # drop (field, year) strata with fewer papers
# These bin edges + the midrank tie convention are duplicated in the M5
# placebo sampler (ref_map/export/build_m5_placebo_seeds.py: BREAKS,
# _flush_stratum). Keep the two in sync if either ever changes.
BREAKS <- c(0, .5, .75, .9, .95, .99, 1)
BIN_LABELS <- c("p0-50", "p50-75", "p75-90", "p90-95", "p95-99", "p99-100")

dat <- read.csv(gzfile(HIST))
cat(sprintf("histogram: %d cells\n", nrow(dat)))

dat <- dat[dat$field > 0 & dat$year >= COHORT_MIN & dat$year <= COHORT_MAX &
           dat$cites >= 0, ]
g <- dat$field * 100000L + dat$year
Nstr <- ave(dat$n, g, FUN = sum)
dat <- dat[Nstr >= MIN_STRATUM, ]
g <- dat$field * 100000L + dat$year

# within-stratum midrank percentile of each citation value (ties -> midpoint)
o <- order(g, dat$cites)
dat <- dat[o, ]; g <- g[o]
cum <- ave(dat$n, g, FUN = cumsum)
Nstr <- ave(dat$n, g, FUN = sum)
dat$midfrac <- (cum - 0.5 * dat$n) / Nstr
dat$bin <- cut(dat$midfrac, BREAKS, labels = BIN_LABELS, include.lowest = TRUE)

cat(sprintf("analysis set: %.0fM papers, %d strata, cohorts %d-%d\n",
    sum(dat$n) / 1e6, length(unique(g)), COHORT_MIN, COHORT_MAX))

# citations-only AUC from ungrouped histogram rows: score = midfrac
auc_from <- function(score, pos, neg) {
  o <- order(score)
  pos <- pos[o]; neg <- neg[o]
  cumneg <- cumsum(as.numeric(neg)) - neg
  sum(pos * (cumneg + 0.5 * neg)) / (sum(as.numeric(pos)) * sum(as.numeric(neg)))
}

# ---- aggregate to (field, year, bin) grouped-binomial cells ----
key <- paste(dat$field, dat$year, dat$bin, sep = "|")
agg <- rowsum(cbind(n = dat$n,
                    dat[, unname(FLAGS), drop = FALSE]), key)
parts <- do.call(rbind, strsplit(rownames(agg), "|", fixed = TRUE))
cells <- data.frame(field = factor(parts[, 1]), year = factor(parts[, 2]),
                    bin = factor(parts[, 3], levels = BIN_LABELS), agg,
                    row.names = NULL)
cat(sprintf("grouped cells: %d\n", nrow(cells)))

CLAMP <- function(p) pmin(pmax(p, 1e-12), 1 - 1e-12)
bern_ll <- function(k, n, p) { p <- CLAMP(p); sum(k * log(p) + (n - k) * log(1 - p)) }

BLOCK_TERMS <- c(field = "field", cohort = "year", citations = "bin")

shapley3 <- function(vfun) {  # exact Shapley over the 3 named blocks
  bn <- names(BLOCK_TERMS); k <- length(bn)
  subs <- function(v) unlist(lapply(0:length(v),
            function(m) combn(v, m, simplify = FALSE)), recursive = FALSE)
  keyf <- function(s) if (length(s) == 0) "__empty__" else paste(sort(s), collapse = "|")
  vmap <- new.env()
  for (s in subs(bn)) assign(keyf(s), vfun(s), envir = vmap)
  phi <- setNames(numeric(k), bn)
  for (b in bn) {
    val <- 0
    for (s in subs(setdiff(bn, b))) {
      w <- factorial(length(s)) * factorial(k - length(s) - 1) / factorial(k)
      val <- val + w * (get(keyf(c(s, b)), vmap) - get(keyf(s), vmap))
    }
    phi[b] <- val
  }
  phi
}

md <- c("# Paper-level absorption regression (c_0 seed set) — grouped binomial",
        "",
        sprintf("Corpus scan: 270.1M works; analysis set: cohorts %d-%d, field known, strata >= %d papers.",
                COHORT_MIN, COHORT_MAX, MIN_STRATUM),
        "Model: glm(cbind(k, n-k) ~ field + cohort + citation-percentile-bin, binomial).",
        "Grouped fit == individual-level Bernoulli likelihood (all predictors discrete).",
        "Percentiles are within (field x year), midrank convention for tied citation counts.",
        "PU caveat: Marx linkage incompleteness lowers absolute rates (lower bounds);",
        "dose-response shape and AUC are preserved under selected-at-random.", "")
shap_rows <- list(); rate_rows <- list(); seq_txt <- c()

for (fl_name in names(FLAGS)) {
  kcol <- FLAGS[[fl_name]]
  k <- cells[[kcol]]; n <- cells$n
  pbar <- sum(k) / sum(n)

  # ---- dose-response ----
  rb_n <- tapply(n, cells$bin, sum); rb_k <- tapply(k, cells$bin, sum)
  rate <- rb_k / rb_n
  for (b in BIN_LABELS)
    rate_rows[[length(rate_rows) + 1]] <- data.frame(
      flag = fl_name, bin = b, n = rb_n[[b]], k = rb_k[[b]],
      rate = rate[[b]], ratio_vs_bottom = rate[[b]] / rate[["p0-50"]])

  # ---- fits ----
  vfun <- function(blocks) {
    if (length(blocks) == 0) return(0)
    f <- as.formula(paste("cbind(k, n - k) ~",
           paste(BLOCK_TERMS[blocks], collapse = " + ")))
    m <- glm(f, family = binomial, data = cbind(cells, k = k))
    1 - bern_ll(k, n, fitted(m)) / bern_ll(k, n, rep(pbar, length(k)))
  }
  sh <- shapley3(vfun); d2_full <- vfun(names(BLOCK_TERMS))
  shap_rows[[length(shap_rows) + 1]] <- data.frame(
    flag = fl_name, t(sh), total_D2 = d2_full)

  m_full <- glm(cbind(k, n - k) ~ field + year + bin, family = binomial,
                data = cbind(cells, k = k))
  p_hat <- fitted(m_full)
  tjur <- sum(k * p_hat) / sum(k) - sum((n - k) * p_hat) / sum(n - k)
  auc_model <- auc_from(p_hat, k, n - k)
  auc_cites <- auc_from(dat$midfrac, dat[[kcol]], dat$n - dat[[kcol]])

  seq_txt <- c(seq_txt, sprintf("\n=== %s (%s): sequential analysis of deviance ===",
               fl_name, kcol),
               capture.output(anova(m_full, test = "LRT")))

  # robustness one-liners
  m_clog <- glm(cbind(k, n - k) ~ field + year + bin,
                family = binomial(link = "cloglog"), data = cbind(cells, k = k))
  d2_clog <- 1 - bern_ll(k, n, fitted(m_clog)) / bern_ll(k, n, rep(pbar, length(k)))
  m_lpm <- lm(I(k / n) ~ field + year + bin, weights = n, data = cbind(cells, k = k))

  md <- c(md, sprintf("## Outcome: %s (`%s`)", fl_name, kcol), "",
    sprintf("Base rate %.4f%% (%s of %s papers).", 100 * pbar,
            format(sum(k), big.mark = ","), format(sum(n), big.mark = ",")), "",
    "| citation bin | n | k | absorption rate | ratio vs p0-50 |",
    "|---|--:|--:|--:|--:|")
  for (b in BIN_LABELS)
    md <- c(md, sprintf("| %s | %s | %s | %.4f%% | %.1f |", b,
      format(rb_n[[b]], big.mark = ","), format(rb_k[[b]], big.mark = ","),
      100 * rate[[b]], rate[[b]] / rate[["p0-50"]]))
  md <- c(md, "",
    sprintf("- **AUC (citations only, within-stratum percentile): %.3f**; AUC (full model): %.3f",
            auc_cites, auc_model),
    sprintf("- Tjur R^2: %.4f; McFadden D^2 (logit): %.3f; D^2 (cloglog): %.3f; LPM weighted R^2: %.3f",
            tjur, d2_full, d2_clog, summary(m_lpm)$r.squared),
    sprintf("- Shapley shares of D^2: field %.3f | cohort %.3f | citations %.3f (total %.3f)",
            sh[["field"]], sh[["cohort"]], sh[["citations"]], d2_full), "")
  cat(sprintf("[%s] %s done: top-bin rate %.3f%%, ratio %.1fx, AUC(cites) %.3f\n",
      format(Sys.time(), "%H:%M:%S"), fl_name, 100 * rate[["p99-100"]],
      rate[["p99-100"]] / rate[["p0-50"]], auc_cites))
}

write.csv(do.call(rbind, shap_rows), file.path(OUT, "paper_absorption_shapley.csv"),
          row.names = FALSE)
write.csv(do.call(rbind, rate_rows), file.path(OUT, "paper_absorption_rates.csv"),
          row.names = FALSE)
writeLines(seq_txt, file.path(OUT, "paper_absorption_seq_deviance.txt"))
writeLines(md, file.path(OUT, "paper_absorption_results.md"))
cat("done. wrote paper_absorption_{results.md,shapley.csv,rates.csv,seq_deviance.txt}\n")
