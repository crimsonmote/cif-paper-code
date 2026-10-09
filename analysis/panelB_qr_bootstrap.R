#!/usr/bin/env Rscript
# Bootstrap CIs for Table 1 Panel B quantile-criterion rows (tau = 0.95).
# Venue-resample bootstrap (B = 200) of the exact 5-block Shapley
# decomposition of Koenker-Machado R1(0.95), same spec as venue_regression.R
# (blocks field/log_size/median_year/age_span/metric; raw outcome cif_norm).
# Writes regression_output/panelB_qr_share_cis.{md,csv}.

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
METRICS <- c(JIF = "jif", SJR = "sjr")
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

md <- c("# Panel B quantile rows (tau = 0.95) — venue-resample bootstrap CIs",
        "", sprintf("B = %d, seed 20260802. Shares in percent.", B), "")
csv_rows <- data.frame()
for (mname in names(METRICS)) {
  pcol <- METRICS[[mname]]
  d <- dat_all[!is.na(dat_all[[pcol]]) & !is.na(dat_all[[OUTCOME]]) &
               !is.na(dat_all$median_year) & !is.na(dat_all$log_size) &
               !is.na(dat_all$age_span), ]
  point <- decomp(d, pcol)
  cat(sprintf("[%s] %s point: %s total=%.3f\n",
      format(Sys.time(), "%H:%M:%S"), mname,
      paste(sprintf("%s=%.3f", BLOCKS, point$phi), collapse = " "),
      point$total)); flush(stdout())
  boots <- matrix(NA_real_, nrow = B, ncol = length(BLOCKS) + 1,
                  dimnames = list(NULL, c(BLOCKS, "total")))
  for (b in seq_len(B)) {
    db <- d[sample.int(nrow(d), nrow(d), replace = TRUE), ]
    res <- tryCatch(decomp(db, pcol), error = function(e) NULL)
    if (is.null(res)) next
    boots[b, ] <- c(res$phi, res$total)
    if (b %% 25 == 0) {
      cat(sprintf("[%s] %s boot %d/%d\n",
          format(Sys.time(), "%H:%M:%S"), mname, b, B)); flush(stdout())
    }
  }
  md <- c(md, sprintf("## %s (n = %s)", mname, format(nrow(d), big.mark = ",")),
          "", "| block | share % | 95% CI |", "|---|--:|---|")
  for (j in c(BLOCKS, "total")) {
    pt <- if (j == "total") point$total else point$phi[[j]]
    qs <- quantile(boots[, j], c(.025, .975), na.rm = TRUE)
    md <- c(md, sprintf("| %s | %.1f | [%.1f, %.1f] |",
            j, 100 * pt, 100 * qs[1], 100 * qs[2]))
    csv_rows <- rbind(csv_rows, data.frame(
      metric = mname, n = nrow(d), tau = TAU, block = j,
      share_pct = 100 * pt, lo95 = 100 * qs[1], hi95 = 100 * qs[2],
      B_ok = sum(!is.na(boots[, j]))))
  }
  md <- c(md, "")
}
writeLines(md, file.path(OUT, paste0("panelB_qr_share_cis_", OUTCOME, ".md")))
write.csv(csv_rows, file.path(OUT, paste0("panelB_qr_share_cis_", OUTCOME, ".csv")),
          row.names = FALSE)
cat("done\n")
