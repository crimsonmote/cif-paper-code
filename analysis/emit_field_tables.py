"""
Emit LaTeX longtables for S3.3:
  * top 5 papers per field (10 fields)
  * top 10 venues per field (10 fields)

Reads figs/top_cif_per_field_enriched.csv and
figs/top_venues_per_field_enriched.csv. Applies the same anomaly curation
described in S3.1: drops the Cellular Physiology 2013 reference-parsing
collision, and drops the OpenAlex record whose title is
"Lecture Notes in Computer Science 1205" (metadata corruption, no
identifiable paper).

Writes:
    figs/top_papers_by_field.tex   longtable body
    figs/top_venues_by_field.tex   longtable body
"""
import os
import csv
import re
from collections import defaultdict

FIGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")
os.makedirs(FIGS, exist_ok=True)
FIELDS = [
    ("fields/22", "Engineering"),
    ("fields/27", "Medicine"),
    ("fields/13", "Biochemistry, Genetics and Molecular Biology"),
    ("fields/17", "Computer Science"),
    ("fields/25", "Materials Science"),
    ("fields/31", "Physics and Astronomy"),
    ("fields/16", "Chemistry"),
    ("fields/24", "Immunology and Microbiology"),
    ("fields/28", "Neuroscience"),
    ("fields/11", "Agricultural and Biological Sciences"),
]

# Titles to exclude from the paper table (per-field curation). Matched as
# a prefix on the OpenAlex title after HTML-tag cleanup.
EXCLUDE_TITLE_PREFIXES = (
    # 2013 reference-parsing collision documented in Section S3.1
    "Short-Term Effects of Nose-Only Cigarette Smoke Exposure",
    # OpenAlex record whose title is just the LNCS volume string
    "Lecture Notes in Computer Science 1205",
)

TOP_N_PAPERS = 5
TOP_N_VENUES = 10

# Excluded from the venue tables: not primary publication venues, but
# secondary abstracting/indexing services that OpenAlex sometimes lists
# as a work's `primary_source`.
EXCLUDE_VENUES = {
    "PubMed",
    "ChemInform",
    "Chemischer Informationsdienst",
}


def latex_escape(s):
    if s is None:
        return ""
    s = s.replace("&", r"\&")
    s = s.replace("_", r"\_")
    s = s.replace("%", r"\%")
    s = s.replace("#", r"\#")
    s = s.replace("$", r"\$")
    return s


DISPLAY_ALIASES = {
    'PLANT PHYSIOLOGY': 'Plant Physiology',
    'SLAS DISCOVERY': 'SLAS Discovery',
    'Proceedings of SPIE, the International Society for Optical Engineering/Proceedings of SPIE': 'Proceedings of SPIE',
    'Methods in enzymology on CD-ROM/Methods in enzymology': 'Methods in Enzymology',
    '2009 IEEE Conference on Computer Vision and Pattern Recognition': 'IEEE Computer Vision and Pattern Recognition',
    'Proceedings - International Conference on Image Processing': 'International Conference on Image Processing',
}


HTML_ENTITIES = {
    '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'",
    '&nbsp;': ' ',
}
TITLE_FIXES = {'cLone': 'clone'}
UNICODE_FIX = {
    '′': "$'$",
    '’': "'",
    '″': "$''$",
    '‘': '`',
    '“': '``',
    '”': "''",
    '–': '--',
    '—': '---',
    '\xa0': ' ',
    '−': '$-$',
    'β': r'$\beta$',
    'α': r'$\alpha$',
    'γ': r'$\gamma$',
    'λ': r'$\lambda$',
    'μ': r'$\mu$',
    '∼': r'$\sim$',
    'ü': r'\"{u}',
    'ö': r'\"{o}',
    'ä': r'\"{a}',
    'é': r"\'{e}",
    '‐': '-',
    '‑': '-',
}


def _fix_unicode(s):
    for _k, _v in HTML_ENTITIES.items():
        s = s.replace(_k, _v)
    for _k, _v in TITLE_FIXES.items():
        s = s.replace(_k, _v)
    for _k, _v in UNICODE_FIX.items():
        s = s.replace(_k, _v)
    return s


def clean_title(t):
    """Normalise an OpenAlex/CrossRef title for LaTeX.

    Publisher-deposited titles carry artefacts that CrossRef reproduces:
    all-caps legacy titles (JBC, Biochem. J.), chapter numbers in the title
    field ("[20] Processing of..."), HTML emphasis markup, and typographic
    Unicode. Normalise all four rather than truncating past them.
    """
    if not t:
        return ""
    t = re.sub(r"^\[\d+\]\s*", "", t.strip())          # publisher chapter numbers
    t = re.sub(r"<i>(.*?)</i>", r"\\emph{\1}", t)         # italics -> emph
    t = re.sub(r"<sub>(.*?)</sub>", r"\\textsubscript{\1}", t)
    t = re.sub(r"<sup>(.*?)</sup>", r"\\textsuperscript{\1}", t)
    t = re.sub(r"<[^>]+>", "", t)                        # any other markup
    t = _fix_unicode(t)
    words = t.split()
    letters = [c for c in t if c.isalpha()]
    if len(words) > 3 and letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        t = t[0].upper() + t[1:].lower()                 # legacy all-caps -> sentence case
        for w in ("folin", "i-labelled", "dna", "rna", "atp", "nmr"):
            t = re.sub(r"\b" + w + r"\b", w.upper() if len(w) <= 3 else w.capitalize(), t)
    for c, r in (("&", r"\&"), ("%", r"\%"), ("#", r"\#"), ("_", r"\_")):
        t = t.replace(c, r)
    return t

UNICODE_REPLACEMENTS = UNICODE_FIX


def escape_unicode(s):
    for u, tex in UNICODE_REPLACEMENTS.items():
        s = s.replace(u, tex)
    return s


def truncate(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def emit_papers_table():
    rows_by_field = defaultdict(list)
    with open(os.path.join(FIGS, "top_cif_per_field_enriched.csv")) as f:
        for r in csv.DictReader(f):
            rows_by_field[r["field_id_query"]].append(r)

    out = []
    out.append(r"% Autogenerated by emit_field_tables.py")
    out.append(r"\footnotesize")
    out.append(r"\begin{longtable}{p{0.36\textwidth} p{0.22\textwidth} l r r c}")
    out.append(r"\caption[Top-CIF articles within each of the top 10 fields]{\textbf{Top-CIF articles within each of the top 10 fields.} Fields are ordered by seed paper count, and papers within each field by CIF. A check mark in the Seed column marks a paper in the industry-absorbed seed set. CIF values are PAR scores from the main text specification. Citations are OpenAlex \texttt{cited\_by\_count}. Each work's field follows its OpenAlex primary topic, which can place a methods paper outside the discipline that uses it.}\label{tab:top-papers-by-field}\\")
    out.append(r"\toprule")
    out.append(r"Title & Venue & Yr. & CIF & Citations & Seed \\")
    out.append(r"\midrule\endfirsthead")
    out.append(r"\multicolumn{6}{l}{\emph{Table~\ref{tab:top-papers-by-field} (continued)}}\\")
    out.append(r"\toprule Title & Venue & Yr. & CIF & Citations & Seed \\ \midrule\endhead")
    out.append(r"\bottomrule\endfoot")

    for fid, fname in FIELDS:
        candidates = rows_by_field[fid]
        curated = []
        for r in candidates:
            t = clean_title(r["title"]).strip()
            if any(t.startswith(p) for p in EXCLUDE_TITLE_PREFIXES):
                continue
            curated.append(r)
        picked = curated[:TOP_N_PAPERS]

        out.append(r"\midrule")
        out.append(rf"\multicolumn{{6}}{{l}}{{\textbf{{{latex_escape(fname)}}}}}\\")
        out.append(r"\midrule")
        for r in picked:
            title = clean_title(r["title"])
            title_tex = title
            # Titles starting with '[' are parsed as \\ optional arg; wrap in {}
            if title_tex.startswith("["):
                title_tex = "{" + title_tex + "}"
            venue = clean_title(DISPLAY_ALIASES.get((r["src_name"] or "(no venue)").strip(), r["src_name"] or "(no venue)"))
            year = r["year"] or ""
            cif = f"{float(r['cif']):.3f}"
            cby = f"{int(r['cited_by_count']):,}" if r["cited_by_count"] else "--"
            seed = r"$\checkmark$" if r["is_seed"] == "True" else ""
            out.append(f"{title_tex} & {venue} & {year} & {cif} & {cby} & {seed} \\\\")

    out.append(r"\end{longtable}")
    out.append(r"\normalsize")

    with open(os.path.join(FIGS, "top_papers_by_field.tex"), "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote figs/top_papers_by_field.tex  ({sum(1 for l in out if '&' in l) - 1} data rows)")


def emit_venues_table():
    rows_by_field = defaultdict(list)
    with open(os.path.join(FIGS, "venues_norm_works_per_field_enriched.csv")) as f:
        for r in csv.DictReader(f):
            if (r.get("src_name") or "").strip() in EXCLUDE_VENUES:
                continue
            if (r.get("src_meta_type") or "") not in ("journal", "conference"):
                continue
            rows_by_field[r["field_id_query"]].append(r)

    out = []
    out.append(r"% Autogenerated by emit_field_tables.py")
    out.append(r"\begin{longtable}{p{0.74\textwidth} r}")
    out.append(r"\caption[Top 10 venues by aggregate CIF within each field]{\textbf{Top 10 venues by aggregate CIF within each field.} Aggregate CIF is $\sum_{w \in F,\, s(w)=v} \mathrm{CIF}(w)$ over works with primary field $F$ and primary source $v$. Journals and conferences only; PubMed, ChemInform, and Chemischer Informationsdienst are excluded, since they index rather than publish.}\label{tab:top-venues-by-field}\\")
    out.append(r"\toprule")
    out.append(r"Venue & Aggregate CIF \\")
    out.append(r"\midrule\endfirsthead")
    out.append(r"\multicolumn{2}{l}{\emph{Table~\ref{tab:top-venues-by-field} (continued)}}\\")
    out.append(r"\toprule Venue & Aggregate CIF \\ \midrule\endhead")
    out.append(r"\bottomrule\endfoot")

    for fid, fname in FIELDS:
        rows = sorted(rows_by_field[fid],
                      key=lambda r: -float(r["cif_sum"]))[:TOP_N_VENUES]
        if not rows:
            continue
        out.append(r"\midrule")
        out.append(rf"\multicolumn{{2}}{{l}}{{\textbf{{{latex_escape(fname)}}}}}\\")
        out.append(r"\midrule")
        for r in rows:
            venue = clean_title(DISPLAY_ALIASES.get((r["src_name"] or "(no venue)").strip(), r["src_name"] or "(no venue)"))
            venue_tex = venue
            csum = f"{float(r['cif_sum']):.2f}"
            out.append(f"{venue_tex} & {csum} \\\\")

    out.append(r"\end{longtable}")

    with open(os.path.join(FIGS, "top_venues_by_field.tex"), "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote figs/top_venues_by_field.tex  ({sum(1 for l in out if '&' in l) - 1} data rows)")


MIN_WORKS_IN_FIELD = 1000   # mirrors the Table S3 normalized-ranking floor
NORM_SCALE = 1e4               # normalized CIF is reported as CIF x 10^-4


def emit_venues_norm_table():
    """Top 10 venues per field by NORMALIZED CIF.

    Unlike the aggregate table, this reads venues_norm_per_field_enriched.csv,
    a full grouped aggregation over every article in each field. The
    aggregate table's top 2M truncation cannot support a per-paper measure:
    its denominator would be the censored count.
    """
    rows_by_field = defaultdict(list)
    with open(os.path.join(FIGS, "venues_norm_works_per_field_enriched.csv")) as f:
        for r in csv.DictReader(f):
            if (r.get("src_name") or "").strip() in EXCLUDE_VENUES:
                continue
            if (r.get("src_meta_type") or "") not in ("journal", "conference"):
                continue
            if int(r["n_articles"]) < MIN_WORKS_IN_FIELD:
                continue
            rows_by_field[r["field_id_query"]].append(r)

    out = []
    out.append(r"% Autogenerated by emit_field_tables.py")
    out.append(r"\begin{longtable}{p{0.56\textwidth} r r}")
    out.append(r"\caption[Top 10 venues by normalized CIF within each field]{\textbf{Top 10 venues by normalized CIF within each field, venues with at least 1{,}000 works in the field.} Normalized CIF is the mean paper-level CIF over the venue's works in the field; those no seed paper reaches count as zero. ``Works'' is that denominator, on the same per-work basis as Table~\ref{tab:top_cif_norm}. Journals and conferences only; PubMed, ChemInform, and Chemischer Informationsdienst are excluded.}\label{tab:top-venues-norm-by-field}\\")
    out.append(r"\toprule")
    out.append(r"Venue & Works & \shortstack[r]{Normalized\\CIF ($10^{-4}$)} \\")
    out.append(r"\midrule\endfirsthead")
    out.append(r"\multicolumn{3}{l}{\emph{Table~\ref{tab:top-venues-norm-by-field} (continued)}}\\")
    out.append(r"\toprule Venue & Works & \shortstack[r]{Normalized\\CIF ($10^{-4}$)} \\ \midrule\endhead")
    out.append(r"\bottomrule\endfoot")

    for fid, fname in FIELDS:
        rows = sorted(rows_by_field[fid], key=lambda r: -float(r["cif_norm"]))[:TOP_N_VENUES]
        if not rows:
            continue
        out.append(r"\midrule")
        out.append(rf"\multicolumn{{3}}{{l}}{{\textbf{{{latex_escape(fname)}}}}}\\")
        out.append(r"\midrule")
        for r in rows:
            venue = clean_title(DISPLAY_ALIASES.get((r["src_name"] or "(no venue)").strip(),
                                                    r["src_name"] or "(no venue)"))
            n = f"{int(r['n_articles']):,}".replace(",", "{,}")
            cn = f"{float(r['cif_norm']) * NORM_SCALE:.1f}"
            out.append(f"{venue} & {n} & {cn} \\\\")

    out.append(r"\end{longtable}")
    with open(os.path.join(FIGS, "top_venues_norm_by_field.tex"), "w") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote figs/top_venues_norm_by_field.tex  ({sum(1 for l in out if '&' in l) - 1} data rows)")


if __name__ == "__main__":
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    emit_papers_table()
    try:
        emit_venues_table()
    except FileNotFoundError as e:
        print(f"(skipping venues: {e})")
    try:
        emit_venues_norm_table()
    except FileNotFoundError as e:
        print(f"(skipping normalized venues: {e})")
