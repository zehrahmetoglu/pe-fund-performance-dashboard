"""
Step 1 - Fetch & clean CalPERS Private Equity fund performance data.

Source : CalPERS "Private Equity Program (PEP) Fund Performance Review"
         https://www.calpers.ca.gov/page/investments/about-investment-office/investment-organization/pep-fund-performance
Output : data/calpers_funds_clean.csv   -> one row per fund, ready for Power BI
         data/pe_funds.db               -> same table in SQLite (table: calpers_funds)
         data/strategy_review.csv       -> funds whose strategy could not be auto-tagged (fill by hand)

Usage  : python 01_fetch_clean_calpers.py                 # downloads the page
         python 01_fetch_clean_calpers.py saved_page.html # uses a page you saved in your browser

Requirements: pip install pandas lxml requests
"""

import re
import sqlite3
import sys
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

URL = ("https://www.calpers.ca.gov/page/investments/about-investment-office/"
       "investment-organization/pep-fund-performance")
OUT = Path("data")


# ---------------------------------------------------------------- 1. LOAD
def load_html(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    headers = {"User-Agent": "Mozilla/5.0 (portfolio project; educational use)"}
    r = requests.get(URL, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def parse_table(html: str) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    # "As of March 31, 2026" -> reporting date
    m = re.search(r"as of\s+([A-Z][a-z]+ \d{1,2}, \d{4})", html, flags=re.I)
    as_of = pd.to_datetime(m.group(1)) if m else None

    tables = pd.read_html(StringIO(html))
    df = next(t for t in tables if "Net IRR" in " ".join(map(str, t.columns)))
    return df, as_of


# ---------------------------------------------------------------- 2. CLEAN
def to_money(s: pd.Series) -> pd.Series:
    # "$1,234,567" -> 1234567.0 ; "($5)" -> -5
    s = s.astype(str).str.strip()
    neg = s.str.startswith("(") & s.str.endswith(")")
    s = s.str.replace(r"[^\d.\-]", "", regex=True)
    out = pd.to_numeric(s, errors="coerce")
    return out.where(~neg, -out)


def split_footnote(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    # "41.3%  1" / "41.3%1" / "1.4x 1" / "N/M 1" -> ("41.3%", True).
    # Footnote 1 marks young funds whose interim numbers aren't meaningful yet.
    s = s.astype(str).str.strip()
    parts = s.str.extract(r"^(?P<val>N/M|-?[\d.,]+\s*[%x]?)\s*(?P<fn>\d+)?$", flags=re.I)
    return parts["val"].fillna(s).str.strip(), parts["fn"].notna()


def clean(df: pd.DataFrame, as_of) -> pd.DataFrame:
    df = df.copy()
    df.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]
    df = df.rename(columns={
        "Fund": "fund_name",
        "Vintage Year": "vintage_year",
        "Capital Committed": "committed",
        "Cash In": "paid_in",
        "Cash Out": "distributions",
        "Cash Out & Remaining Value": "total_value",
        "Net IRR": "net_irr_raw",
        "Investment Multiple": "multiple_raw",
    })

    # drop repeated header rows / empty rows inside the table
    df = df[df["fund_name"].astype(str).str.strip().ne("Fund")]
    df = df[pd.to_numeric(df["vintage_year"], errors="coerce").notna()]

    df["fund_name"] = (df["fund_name"].astype(str)
                       .str.replace("Co?peratief", "Cooperatief", regex=False)
                       .str.replace(r"\s+", " ", regex=True).str.strip())
    df["vintage_year"] = df["vintage_year"].astype(int)

    for c in ["committed", "paid_in", "distributions", "total_value"]:
        df[c] = to_money(df[c])

    irr_txt, fn_irr = split_footnote(df["net_irr_raw"])
    mult_txt, fn_mult = split_footnote(df["multiple_raw"])
    df["net_irr"] = pd.to_numeric(irr_txt.str.replace("%", ""), errors="coerce") / 100  # N/M -> NaN
    df["reported_multiple"] = pd.to_numeric(mult_txt.str.replace("x", ""), errors="coerce")
    df["early_stage_flag"] = fn_irr | fn_mult
    df["irr_meaningful"] = df["net_irr"].notna() & ~df["early_stage_flag"]

    # ---------------------------------------------------------- 3. DERIVE
    df["nav"] = df["total_value"] - df["distributions"]            # remaining (unrealised) value
    pi = df["paid_in"].where(df["paid_in"] > 0)                    # avoid /0
    df["dpi"] = df["distributions"] / pi
    df["rvpi"] = df["nav"] / pi
    df["tvpi"] = df["total_value"] / pi                            # = dpi + rvpi
    df["unfunded"] = (df["committed"] - df["paid_in"]).clip(lower=0)
    df["pct_called"] = df["paid_in"] / df["committed"]             # >1 = recycling / fees
    df["gain_loss"] = df["total_value"] - df["paid_in"]

    if as_of is not None:
        df["as_of_date"] = as_of.date()
        df["fund_age_years"] = as_of.year - df["vintage_year"]

    df["gp_name"] = df["fund_name"].map(gp_from_name)
    df["strategy"] = df["fund_name"].map(strategy_from_name)
    df["vintage_bucket"] = pd.cut(
        df["vintage_year"], bins=[0, 2004, 2009, 2014, 2019, 2022, 2100],
        labels=["<=2004", "2005-09", "2010-14", "2015-19", "2020-22", "2023+"])

    return df.drop(columns=["net_irr_raw", "multiple_raw"]).reset_index(drop=True)


# ---------------------------------------------------------------- 4. TAGGING
# GP = the firm managing the fund. Known prefixes first; otherwise the firm name
# is derived from the fund name by stripping legal suffixes, fund numbers and
# generic words ("Blackstone Capital Partners VII, L.P." -> "Blackstone Capital").
GP_PREFIXES = {
    "Green Equity Investors": "Leonard Green & Partners",
    "Jade Equity": "Leonard Green & Partners",
    "Hellman & Friedman": "Hellman & Friedman",
    "Clayton, Dubilier & Rice": "Clayton, Dubilier & Rice",
    "Advent International": "Advent International",
    "Advent Global": "Advent International",
    "Bain Capital": "Bain Capital",
    "General Catalyst": "General Catalyst",
    "GC Creation": "General Catalyst",
    "GC Customer Value": "General Catalyst",
    "General Atlantic": "General Atlantic",
    "GA Continuity": "General Atlantic",
    "GCM Grosvenor": "GCM Grosvenor",
    "New Mountain": "New Mountain Capital",
    "NM Pacific": "New Mountain Capital",
    "Insight": "Insight Partners",
    "Lightspeed": "Lightspeed Venture Partners",
    "Bessemer": "Bessemer Venture Partners",
    "BVP": "Bessemer Venture Partners",
    "Blackstone": "Blackstone",
    "GSO": "Blackstone",
    "Carlyle": "Carlyle",
    "KKR": "KKR",
    "Accel-KKR": "Accel-KKR",
    "CVC": "CVC Capital Partners",
    "EQT": "EQT",
    "Apollo": "Apollo",
    "Ares": "Ares",
    "Cerberus": "Cerberus",
    "Oaktree": "Oaktree",
    "Clearlake": "Clearlake",
    "Genstar": "Genstar",
    "Francisco Partners": "Francisco Partners",
    "Bridgepoint": "Bridgepoint",
    "BE VI": "Bridgepoint",
    "BDC ": "Bridgepoint",
    "Wigmore Street (BDC": "Bridgepoint",
    "Hg ": "Hg",
    "HongShan": "HongShan",
    "B Capital": "B Capital",
    "Base10": "Base10",
    "Canaan": "Canaan",
    "Forbion": "Forbion",
    "Lux Ventures": "Lux Capital",
    "Mayfield": "Mayfield",
    "MFC Select": "Mayfield",
    "Onex": "Onex",
    "57 Stars": "57 Stars",
    "AACP": "Asia Alternatives",
    "AlpInvest": "AlpInvest Partners",
    "Asia Alternatives": "Asia Alternatives",
    "ASF ": "Ardian",
    "ArcLight": "ArcLight Capital",
    "First Reserve": "First Reserve",
    "Riverstone": "Riverstone",
    "Grain Communications": "Grain Management",
    "GI ": "GI Partners",
    "PAG ": "PAG",
    "MBK": "MBK Partners",
    "Silver Lake": "Silver Lake",
    "SL SPV": "Silver Lake",
    "TPG": "TPG",
    "The Rise Fund": "TPG",
    "Thoma Bravo": "Thoma Bravo",
    "TowerBrook": "TowerBrook",
    "Towerbrook": "TowerBrook",
    "Trident": "Stone Point Capital",
    "Triton": "Triton",
    "Yucaipa": "Yucaipa",
    "WCAS": "Welsh, Carson, Anderson & Stowe",
    "Welsh, Carson": "Welsh, Carson, Anderson & Stowe",
    "Wellspring": "Wellspring Capital",
    "CalPERS Wellspring": "Wellspring Capital",
    "California Community Venture": "CalPERS (FoF)",
    "California Emerging Ventures": "CalPERS (FoF)",
    "California Asia Investors": "CalPERS (FoF)",
    "CalPERS Clean Energy": "CalPERS (FoF)",
    "Sacramento Private Equity": "CalPERS (FoF)",
}

LEGAL_SUFFIX = re.compile(
    r"[,\s]*\b(L\.?\s?P\.?|LLC|S\.?C\.?Sp\.?|S\.?L\.?P\.?|SCS|ILP|PCC|C\.V\.|AB|"
    r"Co.?peratief U\.A\.|Cooperatief U\.A\.|Limited Partnership|a Delaware Limited Partnership)\s*$",
    flags=re.I)
NUM_TOKEN = re.compile(r"[\s,\-]+(?:[IVXLC]+(?:-[A-Z])?|\d+|[A-Z]|'[A-Z]')\.?$")
WORD_TOKEN = re.compile(
    r"\s+(?:Fund|Funds|Partners|Partnership|Investors|Investments?|Opportunit(?:y|ies)|SPV|Holdings|"
    r"Onshore|Supplemental|Institutional|Parallel|Series)\.?$", flags=re.I)


def gp_stem(name: str) -> str:
    s = re.sub(r"\([^)]*\)", " ", name)           # drop "(onshore)", "(A)", "(C)" ...
    s = s.split(" - ")[0].split(" / ")[0]
    for _ in range(6):
        s = LEGAL_SUFFIX.sub("", s).strip(" ,.-")
    prev = None
    while prev != s:
        prev = s
        cand = NUM_TOKEN.sub("", s).strip(" ,.-")    # fund numbers / letters: always strip
        if cand:
            s = cand
        cand = WORD_TOKEN.sub("", s).strip(" ,.-")   # generic words: keep at least 2 words
        if " " in cand:
            s = cand
    s = re.sub(r"\s+", " ", s)
    # "SPRINGBLUE" -> "Springblue", but keep acronyms like "BC CLP" or "CF24XB"
    return " ".join(w.title() if w.isalpha() and w.isupper() and len(w) > 4 else w for w in s.split())


def gp_from_name(name: str) -> str:
    for prefix, gp in sorted(GP_PREFIXES.items(), key=lambda kv: -len(kv[0])):
        if name.startswith(prefix):
            return gp
    return gp_stem(name)


# First matching rule wins, so order matters (most specific first).
STRATEGY_RULES = [
    ("Fund of Funds", r"57 Stars|Capital Link|California Emerging Ventures|GCM Grosvenor|"
                      r"EMAlternatives|AACP|Asia Alternatives|Community Venture"),
    ("Secondaries",   r"Secondar|Continuation|Continuity|AlpInvest|\bASF\b|Wellspring"),
    ("Co-Investment", r"Co-?Invest|Coinvest"),
    ("Credit / Special Sits", r"Credit|Special Situations|Special Opportunities|Distress|"
                              r"Principal Finance|Oaktree|GSO|Cerberus|Strategic Partners|Recovery"),
    ("Venture Capital", r"Venture|Seed|Lightspeed|Canaan|Bessemer|BVP|Balderton|"
                        r"Base10|Lux Ventures|Mayfield|MFC Select|General Catalyst|HongShan|"
                        r"Forbion|Acrew|BOND|Hedosophia|Biogeneration|Entrepreneurs First|Inception"),
    ("Growth Equity", r"Growth|General Atlantic|B Capital|Insight|Oak HC/FT"),
    ("Buyout", r"Advent|Apollo Investment|Blackstone Capital Partners|Carlyle (Partners|Europe|Asia Partners)|"
               r"KKR|CVC (Capital Partners|European)|EQT|Hellman|Bain Capital (Europe|Asia)|Clayton|"
               r"Clearlake Capital|Genstar Capital|Green Equity|New Mountain Partners|Onex|Berkshire|"
               r"Francisco Partners|Bridgepoint|\bBE VI\b|Hg (Genesis|Saturn|Mercury)|Permira|Ares Corporate|"
               r"Arlington|American Industrial|Accel-KKR|Lindsay Goldberg|Centerbridge|Birch Hill|"
               r"PAG Asia|MBK|Palladium|Greenbriar|One Rock|CapVest|Arsenal|Grain|Ember|GI |"
               r"Tactical Opportunities|Core Equity|First Reserve|ArcLight|Lime Rock|Apollo"),
]


def strategy_from_name(name: str) -> str:
    for label, pattern in STRATEGY_RULES:
        if re.search(pattern, name, flags=re.I):
            return label
    return "Unclassified"


def apply_overrides(df: pd.DataFrame, path: Path = Path("strategy_overrides.csv")) -> pd.DataFrame:
    # Manual mapping for funds the rules can't tag (fund_name, gp_name, strategy, confidence).
    df["classification"] = "rule"
    if not path.exists():
        print(f"(no {path} found - only rule-based tagging applied)")
        return df
    ov = pd.read_csv(path, keep_default_na=False).drop_duplicates("fund_name").set_index("fund_name")
    hit = df["fund_name"].isin(ov.index)
    df.loc[hit, "gp_name"] = df.loc[hit, "fund_name"].map(ov["gp_name"])
    df.loc[hit, "strategy"] = df.loc[hit, "fund_name"].map(ov["strategy"])
    df.loc[hit, "classification"] = df.loc[hit, "fund_name"].map(lambda n: f"manual ({ov.at[n, 'confidence'] or 'n/a'})")
    print(f"Overrides    : applied to {hit.sum()} funds from {path}")
    return df


# ---------------------------------------------------------------- 5. RUN
def main():
    html = load_html(sys.argv[1] if len(sys.argv) > 1 else None)
    raw, as_of = parse_table(html)
    df = apply_overrides(clean(raw, as_of))

    OUT.mkdir(exist_ok=True)
    df.to_csv(OUT / "calpers_funds_clean.csv", index=False)
    with sqlite3.connect(OUT / "pe_funds.db") as con:
        df.to_sql("calpers_funds", con, if_exists="replace", index=False)
    review = df.loc[df["strategy"] == "Unclassified", ["fund_name", "gp_name", "vintage_year"]]
    review.assign(strategy="").to_csv(OUT / "strategy_review.csv", index=False)

    # quick sanity report
    pooled_tvpi = df["total_value"].sum() / df["paid_in"].sum()
    pooled_dpi = df["distributions"].sum() / df["paid_in"].sum()
    print(f"As of        : {as_of.date() if as_of is not None else 'unknown'}")
    print(f"Funds        : {len(df)}  (vintages {df.vintage_year.min()}-{df.vintage_year.max()})")
    print(f"Committed    : ${df.committed.sum()/1e9:,.1f}B | Paid-in ${df.paid_in.sum()/1e9:,.1f}B | "
          f"NAV ${df.nav.sum()/1e9:,.1f}B")
    print(f"Pooled TVPI  : {pooled_tvpi:.2f}x | Pooled DPI {pooled_dpi:.2f}x")
    print(f"IRR N/M      : {df.net_irr.isna().sum()} funds | early-stage flagged: {df.early_stage_flag.sum()}")
    print(f"Unclassified : {len(review)} funds -> fill strategy in data/strategy_review.csv")
    print("\nStrategy mix:\n", df.strategy.value_counts().to_string())


if __name__ == "__main__":
    main()
