#!/usr/bin/env Rscript
# Panel B h-index rows, quantile criterion (tau = 0.95): five-block
# Shapley decomposition of Koenker-Machado R1(0.95) with the OpenAlex
# journal h-index (oa_h) as the metric, venue-resample bootstrap B = 200
# (Panel B standards; same spec as panelB_qr_bootstrap.R).
# Samples: JIF-restricted and SJR-restricted.
# Usage: Rscript panelB_qr_hindex.R <cif_norm|cif_agg>
# Writes regression_output/panelB_qr_hindex_cis_<outcome>.{md,csv}.

suppressMessages(library(quantreg))
set.seed(20260802)

HERE <- tryCatch(dirname(sub("^--file=", "",
          grep("^--file=", commandArgs(FALSE), value = TRUE))),
          error = function(e) ".")
if (length(HERE) == 0 || HERE == "") HERE <- "."
OUT <- file.path(HERE, "regression_output")
dat_all <- read.csv(file.path(OUT, "venue_analysis_table.csv"),
                    na.strings = c("", "NA"), stringsAsFactors = FALSE)
dat_all$age_span <- dat_all$last_year - dat_all$first_year

TAU <- 0.95
OUTCOME <- if (length(commandArgs(trailingOnly = TRUE))) commandArgs(trailingOnly = TRUE)[1] else "cif_norm"
B <- 200
PCOL <- "oa_h"
SAMPLES <- c(`JIF-sample` = "jif", `SJR-sample` = "sjr")
BLOCKS <- c("field", "size", "vintage", "age", "prestige")

check_loss <- function(u, tau) sum(u * (tau - (u < 0)))

subsets_of <- function(v) unlist(lapply(0:length(v),
  function(m) combn(v, m, simplify = FALSE)), recursive = FALSE)
KEY <- function(s) if (length(s) == 0) "__empty__" else paste(sort(s), collapse = "|")

shapley_from_vmap <- function(vmap) {
  phi <- setNames(numeric(length(BLOCKS)), BLOCKS)
  k <- length(BLOCKS)
  for (b in BLOCKS) {
    others <- setdiff(BLOCKS, b)
    val <- 0
    for (s in subsets_of(others)) {
      w <- factorial(length(s)) * factorial(k - length(s) - 1) / factorial(k)
      val <- val + w * (vmap[[KEY(c(s, b))]] - vmap[[KEY(s)]])
    }
    phi[b] <- val
  }
  phi
}

decomp <- function(d, pcol) {
  bt <- list(field = "factor(primary_field)", size = "log_size",
             vintage = "median_year", age = "age_span", prestige = pcol)
  m0 <- rq(as.formula(paste(OUTCOME, "~ 1")), tau = TAU, data = d, method = "fn")
  V0 <- check_loss(residuals(m0), TAU)
  vmap <- list("__empty__" = 0)
  for (s in subsets_of(BLOCKS)) {
    if (length(s) == 0) next
    f <- as.formula(paste(OUTCOME, "~", paste(unlist(bt[s]), collapse = " + ")))
    m <- rq(f, tau = TAU, data = d, method = "fn")
    vmap[[KEY(s)]] <- 1 - check_loss(residuals(m), TAU) / V0
  }
  list(phi = shapley_from_vmap(vmap), total = vmap[[KEY(BLOCKS)]])
}

md <- c(sprintf("# Panel B h-index quantile rows (tau = %.2f) — bootstrap CIs", TAU),
        "", sprintf("Metric oa_h; outcome %s; B = %d, seed 20260802. Shares in percent.",
                    OUTCOME, B), "")
csvr <- data.frame()

for (sname in names(SAMPLES)) {
  cond <- SAMPLES[[sname]]
  d <- dat_all[!is.na(dat_all[[cond]]) & !is.na(dat_all[[PCOL]]) &
               !is.na(dat_all[[OUTCOME]]) & !is.na(dat_all$log_size) &
               !is.na(dat_all$median_year) & !is.na(dat_all$age_span) &
               !is.na(dat_all$primary_field) & dat_all$primary_field != "", ]
  cat(sprintf("[%s] %s n = %d\n", format(Sys.time(), "%H:%M:%S"), sname, nrow(d)))
  pt <- decomp(d, PCOL)
  boots <- matrix(NA_real_, nrow = B, ncol = length(BLOCKS) + 1,
                  dimnames = list(NULL, c(BLOCKS, "total")))
  for (bb in seq_len(B)) {
    db <- d[sample.int(nrow(d), nrow(d), replace = TRUE), ]
    r <- decomp(db, PCOL)
    boots[bb, BLOCKS] <- r$phi
    boots[bb, "total"] <- r$total
    if (bb %% 25 == 0) cat(sprintf("[%s] %s boot %d/%d\n",
      format(Sys.time(), "%H:%M:%S"), sname, bb, B))
  }
  md <- c(md, sprintf("## %s — h-index, %s (n = %s)", OUTCOME, sname,
                      format(nrow(d), big.mark = ",")), "",
          "| block | share % | 95% CI |", "|---|--:|---|")
  for (j in c(BLOCKS, "total")) {
    p <- if (j == "total") pt$total else pt$phi[[j]]
    q <- quantile(boots[, j], c(.025, .975), na.rm = TRUE)
    md <- c(md, sprintf("| %s | %.1f | [%.1f, %.1f] |",
                        j, 100 * p, 100 * q[1], 100 * q[2]))
    csvr <- rbind(csvr, data.frame(outcome = OUTCOME, sample = sname,
      n = nrow(d), tau = TAU, block = j, share_pct = 100 * p,
      lo95_pct = 100 * q[1], hi95_pct = 100 * q[2]))
  }
  md <- c(md, "")
}

writeLines(md, file.path(OUT, sprintf("panelB_qr_hindex_cis_%s.md", OUTCOME)))
write.csv(csvr, file.path(OUT, sprintf("panelB_qr_hindex_cis_%s.csv", OUTCOME)),
          row.names = FALSE)
cat("done\n")
