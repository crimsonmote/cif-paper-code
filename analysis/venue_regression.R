#!/usr/bin/env Rscript
# Stage 2 of the venue-level regression pipeline.
#
# Reads regression_output/venue_analysis_table.csv (built by
# build_venue_analysis_table.py) and fits, for each prestige metric on its
# own sample:
#
#   (1) ANCOVA branch  : lm( asinh(CIF * s) ~ field + log_size + median_year
#                            + age_span + asinh(prestige) ), with an
#                        order-independent block-Shapley decomposition of R^2
#                        (blocks: field, size, vintage, age, prestige) + the
#                        sequential ANOVA. Run at two outcome scalings s:
#                        s=1 ("legacy", method-identical to run 1) and s=1e4
#                        ("rescaled"): cif_norm < 1 makes asinh(x) ~ x, so
#                        s=1 is effectively a raw-magnitude fit; s=1e4
#                        restores the intended log-like compression.
#   (2) QR branch      : rq( CIF ~ field + log_size + median_year + age_span
#                            + prestige ) at tau in {.5,.75,.9,.95}, with a
#                        block-Shapley decomposition of the Koenker-Machado
#                        R1(tau). No scaling needed: R1(tau) is invariant to
#                        positive rescaling of y.
#   (3) two-part (opt) : logistic Pr(CIF>0) + lm on positives.
#
# Blocks are decomposed by an exact Shapley value over the 5 predictor blocks
# (32 subset fits), which handles the categorical field as a single block and
# is order-independent (unlike sequential SS). age_span = last_year -
# first_year (operating span) documents its contribution separately from
# vintage = median_year.
# Field enters as a factor (argmax primary field) by default; set
# FIELD_SPEC="vector" for the full field-share robustness.
#
# Outputs (regression_output/):
#   ancova_shapley.csv (now with a `scale` column), qr_r1_shapley.csv,
#   sequential_anova.txt (rescaled outcome), twopart.txt,
#   venue_regression_results.md, sessionInfo.txt
#
# Reproducible: fixed inputs, no RNG needed (QR/lm are deterministic).

suppressMessages({
  library(quantreg)
})

HERE <- tryCatch(dirname(sub("^--file=", "",
          grep("^--file=", commandArgs(FALSE), value = TRUE))),
          error = function(e) ".")
if (length(HERE) == 0 || HERE == "") HERE <- "."
OUT <- file.path(HERE, "regression_output")
TABLE <- file.path(OUT, "venue_analysis_table.csv")

# ---- config ----
OUTCOMES  <- c(cif_norm = "cif_norm", cif_agg = "cif_agg")  # primary, secondary
PRESTIGE  <- c(JIF = "jif", SJR = "sjr", OpenAlex_h = "oa_h", Scimago_h = "scimago_h")
TAUS      <- c(0.5, 0.75, 0.9, 0.95)
VINTAGE   <- "median_year"        # primary; era of the venue's papers
AGE       <- "age_span"           # operating span = last_year - first_year
CIF_SCALES <- c(legacy = 1, rescaled = 1e4)  # ANCOVA outcome asinh(CIF*s)
FIELD_SPEC <- "factor"            # "factor" (argmax FE) or "vector" (share cols)
RUN_TWOPART <- FALSE              # two-part hurdle: robustness only, off by default

dat_all <- read.csv(TABLE, na.strings = c("", "NA"), stringsAsFactors = FALSE)
dat_all$age_span <- dat_all$last_year - dat_all$first_year
cat(sprintf("loaded %d venues, %d columns\n", nrow(dat_all), ncol(dat_all)))

fs_cols <- grep("^fs_", names(dat_all), value = TRUE)

field_term <- function() {
  if (FIELD_SPEC == "vector") paste(fs_cols[-1], collapse = " + ")  # drop 1 (compositional)
  else "factor(primary_field)"
}

# block name -> model term(s). prestige/outcome handled per-call.
block_terms <- function(prestige_col, asinh_prestige) {
  pv <- if (asinh_prestige) sprintf("asinh(%s)", prestige_col) else prestige_col
  list(field = field_term(), size = "log_size", vintage = VINTAGE,
       age = AGE, prestige = pv)
}

check_loss <- function(u, tau) sum(u * (tau - (u < 0)))

# value function: R^2 (lm) or R1(tau) (qr) for a subset of block names
make_vfun <- function(dat, outcome, bterms, method = c("lm", "qr"),
                      tau = NULL, scale = 1) {
  method <- match.arg(method)
  lhs <- if (method == "lm") {
    if (scale == 1) sprintf("asinh(%s)", outcome)
    else sprintf("asinh(%s * %g)", outcome, scale)
  } else outcome
  V0 <- NULL
  if (method == "qr") {  # intercept-only baseline: compute once, not per subset
    m0 <- rq(as.formula(paste(outcome, "~ 1")), tau = tau, data = dat, method = "fn")
    V0 <- check_loss(residuals(m0), tau)
  }
  function(blocks) {
    rhs <- if (length(blocks) == 0) "1" else
      paste(unlist(bterms[blocks]), collapse = " + ")
    f <- as.formula(paste(lhs, "~", rhs))
    if (method == "lm") {
      summary(lm(f, data = dat))$r.squared
    } else {
      m  <- rq(f, tau = tau, data = dat, method = "fn")
      1 - check_loss(residuals(m), tau) / V0
    }
  }
}

# exact Shapley over block names using a value function v()
shapley <- function(vfun, block_names) {
  k <- length(block_names)
  subs <- function(v) unlist(lapply(0:length(v),
            function(m) combn(v, m, simplify = FALSE)), recursive = FALSE)
  key <- function(s) if (length(s) == 0) "__empty__" else paste(sort(s), collapse = "|")
  allS <- subs(block_names)
  vmap <- new.env()
  for (s in allS) assign(key(s), vfun(s), envir = vmap)
  phi <- setNames(numeric(k), block_names)
  for (b in block_names) {
    others <- setdiff(block_names, b)
    val <- 0
    for (s in subs(others)) {
      w <- factorial(length(s)) * factorial(k - length(s) - 1) / factorial(k)
      val <- val + w * (get(key(c(s, b)), vmap) - get(key(s), vmap))
    }
    phi[b] <- val
  }
  phi  # sums to full-model R^2 / R1
}

BLOCKS <- c("field", "size", "vintage", "age", "prestige")

ancova_rows <- list(); qr_rows <- list(); seq_txt <- c(); twopart_txt <- c()

for (oc_name in names(OUTCOMES)) {
  outcome <- OUTCOMES[[oc_name]]
  for (pr_name in names(PRESTIGE)) {
    pcol <- PRESTIGE[[pr_name]]
    d <- dat_all[!is.na(dat_all[[pcol]]) & !is.na(dat_all[[outcome]]) &
                 !is.na(dat_all[[VINTAGE]]) & !is.na(dat_all$log_size) &
                 !is.na(dat_all[[AGE]]), ]
    if (nrow(d) < 50) next
    bt <- block_terms(pcol, asinh_prestige = TRUE)

    # ---- ANCOVA: Shapley on R^2, at both outcome scalings ----
    for (sc_name in names(CIF_SCALES)) {
      sc <- CIF_SCALES[[sc_name]]
      v_lm <- make_vfun(d, outcome, bt, method = "lm", scale = sc)
      sh <- shapley(v_lm, BLOCKS)
      full_r2 <- v_lm(BLOCKS)
      for (b in BLOCKS)
        ancova_rows[[length(ancova_rows) + 1]] <- data.frame(
          outcome = oc_name, prestige = pr_name, scale = sc_name, n = nrow(d),
          block = b, shapley_r2 = sh[[b]], full_r2 = full_r2)
      cat(sprintf("[%s] ancova %s x %-10s (%s)  R2=%.3f\n",
                  format(Sys.time(), "%H:%M:%S"), oc_name, pr_name, sc_name, full_r2))
      flush(stdout())
    }

    # sequential ANOVA (field -> size -> vintage -> age -> prestige), rescaled
    f_full <- as.formula(paste(
      sprintf("asinh(%s * %g)", outcome, CIF_SCALES[["rescaled"]]), "~",
      paste(unlist(bt[BLOCKS]), collapse = " + ")))
    seq_txt <- c(seq_txt, sprintf(
      "\n=== asinh(%s * %g) ~ ... (prestige=%s, n=%d) ===",
      oc_name, CIF_SCALES[["rescaled"]], pr_name, nrow(d)),
      capture.output(anova(lm(f_full, data = d))))

    # ---- QR: Shapley on R1(tau) ----
    bt_q <- block_terms(pcol, asinh_prestige = FALSE)
    for (tau in TAUS) {
      v_qr <- make_vfun(d, outcome, bt_q, method = "qr", tau = tau)
      shq <- shapley(v_qr, BLOCKS)
      full_r1 <- v_qr(BLOCKS)
      for (b in BLOCKS)
        qr_rows[[length(qr_rows) + 1]] <- data.frame(
          outcome = oc_name, prestige = pr_name, tau = tau, n = nrow(d),
          block = b, shapley_r1 = shq[[b]], full_r1 = full_r1)
      cat(sprintf("[%s] qr     %s x %-10s tau=%.2f  R1=%.3f\n",
                  format(Sys.time(), "%H:%M:%S"), oc_name, pr_name, tau, full_r1))
      flush(stdout())
    }

    # ---- two-part (robustness only; off by default; normalized outcome) ----
    if (RUN_TWOPART && oc_name == "cif_norm") {
      d$pos <- as.integer(d[[outcome]] > 0)
      g <- glm(as.formula(paste("pos ~",
             paste(unlist(bt[BLOCKS]), collapse = " + "))),
             data = d, family = binomial)
      mcfad <- 1 - g$deviance / g$null.deviance
      dp <- d[d[[outcome]] > 0, ]
      lm_pos <- lm(as.formula(paste(sprintf("asinh(%s)", outcome), "~",
                   paste(unlist(bt[BLOCKS]), collapse = " + "))), data = dp)
      twopart_txt <- c(twopart_txt, sprintf(
        "prestige=%s: Pr(CIF>0) logistic McFadden R2=%.3f (n=%d, %d positive); magnitude lm R2=%.3f",
        pr_name, mcfad, nrow(d), sum(d$pos), summary(lm_pos)$r.squared))
    }
  }
}

ancova <- do.call(rbind, ancova_rows)
qr <- do.call(rbind, qr_rows)
write.csv(ancova, file.path(OUT, "ancova_shapley.csv"), row.names = FALSE)
write.csv(qr, file.path(OUT, "qr_r1_shapley.csv"), row.names = FALSE)
writeLines(seq_txt, file.path(OUT, "sequential_anova.txt"))
if (RUN_TWOPART) writeLines(twopart_txt, file.path(OUT, "twopart.txt"))

# ---- readable markdown summary ----
md <- c("# Venue-level regression — ANCOVA + QR (block-Shapley)", "",
        sprintf("Field spec: **%s**; vintage: **%s**; age: **%s** (last - first year).",
                FIELD_SPEC, VINTAGE, AGE),
        "Blocks: field, size, vintage, age, prestige — exact Shapley (32 subsets),",
        "order-independent; shares sum to the full-model R^2 / R1(tau).",
        "ANCOVA outcome asinh(CIF*s) at s=1 (legacy) and s=1e4 (rescaled);",
        "QR on the raw outcome (R1 invariant to positive rescaling).", "")
for (sc_name in names(CIF_SCALES)) {
  md <- c(md, sprintf("## ANCOVA (%s: outcome asinh(CIF*%g)): Shapley share of R^2 by block",
                      sc_name, CIF_SCALES[[sc_name]]), "",
          "| outcome | prestige | n | field | size | vintage | age | prestige | total R2 |",
          "|---|---|--:|--:|--:|--:|--:|--:|--:|")
  for (oc in names(OUTCOMES)) for (pr in names(PRESTIGE)) {
    s <- ancova[ancova$outcome == oc & ancova$prestige == pr &
                ancova$scale == sc_name, ]
    if (nrow(s) == 0) next
    g <- function(b) s$shapley_r2[s$block == b]
    md <- c(md, sprintf("| %s | %s | %d | %.3f | %.3f | %.3f | %.3f | %.3f | %.3f |",
            oc, pr, s$n[1], g("field"), g("size"), g("vintage"), g("age"),
            g("prestige"), s$full_r2[1]))
  }
  md <- c(md, "")
}
md <- c(md, "## QR: Shapley share of Koenker-Machado R1(tau) by block", "",
        "| outcome | prestige | tau | n | field | size | vintage | age | prestige | total R1 |",
        "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|")
for (oc in names(OUTCOMES)) for (pr in names(PRESTIGE)) for (tau in TAUS) {
  s <- qr[qr$outcome == oc & qr$prestige == pr & qr$tau == tau, ]
  if (nrow(s) == 0) next
  g <- function(b) s$shapley_r1[s$block == b]
  md <- c(md, sprintf("| %s | %s | %.2f | %d | %.3f | %.3f | %.3f | %.3f | %.3f | %.3f |",
          oc, pr, tau, s$n[1], g("field"), g("size"), g("vintage"), g("age"),
          g("prestige"), s$full_r1[1]))
}
if (RUN_TWOPART && length(twopart_txt) > 0)
  md <- c(md, "", "## Two-part (cif_norm): zero-vs-positive + magnitude", "", twopart_txt)
writeLines(md, file.path(OUT, "venue_regression_results.md"))

writeLines(capture.output(sessionInfo()), file.path(OUT, "sessionInfo.txt"))
cat("done. wrote ancova_shapley.csv, qr_r1_shapley.csv, sequential_anova.txt,",
    "twopart.txt, venue_regression_results.md\n")
