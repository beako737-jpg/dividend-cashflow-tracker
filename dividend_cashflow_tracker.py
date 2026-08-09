"""Turn one or more brokers' official "配当金・分配金履歴" (dividend/distribution
history) CSVs into a self-contained, offline-viewable HTML report showing how
much dividend/distribution cash has actually landed in your accounts -- by
month, by year, by security, and by account type (NISA growth-frame / NISA
tsumitate-frame / specific / general).

This is a different question from stock-tax-summary's: that tool folds
dividend income into a single number for tax-filing purposes (loss offsetting
across accounts). This tool instead treats dividends as an asset-building
*cash flow* -- the same kind of question dividend investors track by hand in
a spreadsheet or by clicking through each broker's history screen one at a
time. Every major Japanese broker's own site can show you a single account's
history, but none of them combine multiple brokers, and none of them split
the total into "how much of this was tax-free NISA money vs. already-taxed
specific/general-account money" -- that distinction matters because NISA
dividends are received tax-free while specific/general-account dividends have
already had ~20.315% withholding tax deducted before the "receipt amount"
column you see.

Usage:
    python dividend_cashflow_tracker.py --config config.json --out report.html

--config points at a JSON file shaped like this:

    {
      "as_of_year": 2026,                     // optional, default: latest year seen in the data
      "sources": [
        {
          "broker": "SBI証券",                 // label written into the report (optional, default: csv filename)
          "csv": "sbi_dividends.csv",           // path, resolved relative to the config file
          "mapping": { "date": "...", ... },    // optional, overrides DEFAULT_MAPPING
          "datetime_format": "auto",            // "auto" or a strptime pattern
          "encoding": "utf-8-sig"
        },
        ...
      ]
    }

Only include mapping keys that differ from DEFAULT_MAPPING (the same
column-mapping pattern used by broker-csv-consolidator / nisa-quota-tracker /
household-budget-visualizer / subscription-detector). The "code" mapping key
is optional -- set it to "" (or omit the column from your CSV) for brokers
whose dividend history doesn't include a ticker/fund code; the security is
then grouped by name only.

Unlike nisa-quota-tracker (which only cares about NISA-frame transactions
and deliberately skips everything else), this tool keeps every row: the
whole point is a complete picture of received cash. Rows whose account-type
column doesn't match a recognized label are still counted in every total,
just bucketed as "不明" (unknown) -- a warning listing the unrecognized raw
labels is printed so you can extend NISA_GROWTH_LABELS / NISA_TSUMITATE_LABELS
/ NISA_GENERAL_LABELS / SPECIFIC_LABELS / GENERAL_LABELS below if your
broker uses different wording.

This tool intentionally stops at summarizing *already-received* dividends and
distributions. It does not estimate future dividend income, does not project
yield-on-cost forward, and does not recommend which high-dividend stocks to
buy -- only past, confirmed receipts count.
"""
import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DEFAULT_MAPPING = {
    "date": "受渡日",
    "code": "銘柄コード",
    "name": "銘柄名",
    "account": "口座",
    "amount": "受取額（税引後）",
}

AUTO_DATETIME_FORMATS = [
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d",
    "%Y年%m月%d日",
    "%m/%d/%Y",
]

NISA_GROWTH_LABELS = {"NISA（成長投資枠）", "NISA(成長投資枠)", "NISA成長投資枠", "成長投資枠"}
NISA_TSUMITATE_LABELS = {
    "NISA（つみたて投資枠）", "NISA(つみたて投資枠)", "NISAつみたて投資枠", "つみたて投資枠", "つみたてNISA",
}
NISA_GENERAL_LABELS = {"NISA", "一般NISA", "旧NISA"}
SPECIFIC_LABELS = {"特定", "特定口座", "特定預り"}
GENERAL_LABELS = {"一般", "一般口座", "一般預り"}

# Canonical bucket order -- also determines chart stacking / legend order.
ACCOUNT_ORDER = ["nisa_growth", "nisa_tsumitate", "nisa_general", "specific", "general", "unknown"]
ACCOUNT_META = {
    "nisa_growth": {"label": "NISA（成長投資枠）", "taxable": False},
    "nisa_tsumitate": {"label": "NISA（つみたて投資枠）", "taxable": False},
    "nisa_general": {"label": "NISA（区分不明）", "taxable": False},
    "specific": {"label": "特定口座", "taxable": True},
    "general": {"label": "一般口座", "taxable": True},
    "unknown": {"label": "口座区分 不明", "taxable": None},
}


def normalize_date(raw: str, fmt: str, broker: str) -> str:
    raw = raw.strip()
    if fmt and fmt != "auto":
        return datetime.strptime(raw, fmt).date().isoformat()
    for candidate in AUTO_DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, candidate).date().isoformat()
        except ValueError:
            continue
    raise ValueError(
        f"[{broker}] could not parse date value {raw!r}. "
        f"Set an explicit \"datetime_format\" (strptime pattern) in --config."
    )


def _to_float(raw) -> float:
    if raw is None or str(raw).strip() == "":
        return 0.0
    return float(str(raw).replace(",", "").replace("円", ""))


def normalize_account(raw: str) -> str:
    v = (raw or "").strip()
    if v in NISA_GROWTH_LABELS:
        return "nisa_growth"
    if v in NISA_TSUMITATE_LABELS:
        return "nisa_tsumitate"
    if v in NISA_GENERAL_LABELS:
        return "nisa_general"
    if v in SPECIFIC_LABELS:
        return "specific"
    if v in GENERAL_LABELS:
        return "general"
    return "unknown"


def load_source_csv(entry: dict, base_dir: Path) -> tuple:
    if "csv" not in entry:
        raise ValueError(f"config entry missing required \"csv\" key: {entry}")

    csv_path = Path(entry["csv"])
    if not csv_path.is_absolute():
        csv_path = base_dir / csv_path

    broker = entry.get("broker") or csv_path.stem
    mapping = dict(DEFAULT_MAPPING)
    mapping.update(entry.get("mapping") or {})
    datetime_format = entry.get("datetime_format", "auto")
    encoding = entry.get("encoding", "utf-8-sig")

    date_col = mapping.get("date")
    code_col = mapping.get("code")
    name_col = mapping.get("name")
    account_col = mapping.get("account")
    amount_col = mapping.get("amount")
    if not date_col or not name_col or not account_col or not amount_col:
        raise ValueError(f"[{broker}] mapping must include non-empty \"date\", \"name\", \"account\" and \"amount\" columns")

    records = []
    unknown_labels = Counter()
    skipped_blank = 0
    with open(csv_path, encoding=encoding) as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        required = [("date", date_col), ("name", name_col), ("account", account_col), ("amount", amount_col)]
        if code_col:
            required.append(("code", code_col))
        for col, field in required:
            if field not in header:
                raise KeyError(f"[{broker}] column {field!r} for field {col!r} not found in {csv_path} (available: {header})")

        for row in reader:
            raw_date = row.get(date_col)
            if not raw_date or not raw_date.strip():
                skipped_blank += 1
                continue
            raw_name = (row.get(name_col) or "").strip()
            if not raw_name:
                skipped_blank += 1
                continue
            date = normalize_date(raw_date, datetime_format, broker)
            raw_account = (row.get(account_col) or "").strip()
            account_type = normalize_account(raw_account)
            if account_type == "unknown" and raw_account:
                unknown_labels[raw_account] += 1
            amount = round(_to_float(row.get(amount_col)))
            code = (row.get(code_col) or "").strip() if code_col else ""
            records.append(
                {
                    "date": date,
                    "year": int(date[:4]),
                    "month": date[:7],
                    "broker": broker,
                    "code": code,
                    "name": raw_name,
                    "account_type": account_type,
                    "amount": amount,
                }
            )
    return records, skipped_blank, unknown_labels


def compute_report_data(records: list, as_of_year: int) -> dict:
    grand_total = sum(r["amount"] for r in records)

    def pct(part, whole):
        return round((part / whole) * 100, 1) if whole else 0.0

    # --- securities (grouped by name; code is carried along for display) ---
    sec_map = {}
    for r in records:
        s = sec_map.setdefault(r["name"], {"name": r["name"], "code": "", "total": 0, "record_count": 0, "brokers": set()})
        if not s["code"] and r["code"]:
            s["code"] = r["code"]
        s["total"] += r["amount"]
        s["record_count"] += 1
        s["brokers"].add(r["broker"])
    securities = sorted(
        (
            {
                "name": s["name"],
                "code": s["code"],
                "total": s["total"],
                "pct": pct(s["total"], grand_total),
                "record_count": s["record_count"],
                "brokers": sorted(s["brokers"]),
            }
            for s in sec_map.values()
        ),
        key=lambda s: -s["total"],
    )

    # --- account-type breakdown ---
    acc_totals = defaultdict(int)
    acc_counts = defaultdict(int)
    for r in records:
        acc_totals[r["account_type"]] += r["amount"]
        acc_counts[r["account_type"]] += 1
    account_breakdown = sorted(
        (
            {
                "account_type": k,
                "label": ACCOUNT_META[k]["label"],
                "taxable": ACCOUNT_META[k]["taxable"],
                "total": acc_totals[k],
                "pct": pct(acc_totals[k], grand_total),
                "record_count": acc_counts[k],
            }
            for k in ACCOUNT_ORDER
            if acc_counts[k] > 0
        ),
        key=lambda a: -a["total"],
    )
    nisa_total = sum(v for k, v in acc_totals.items() if ACCOUNT_META[k]["taxable"] is False)
    taxable_total = sum(v for k, v in acc_totals.items() if ACCOUNT_META[k]["taxable"] is True)
    unknown_total = sum(v for k, v in acc_totals.items() if ACCOUNT_META[k]["taxable"] is None)

    # --- monthly totals (only months that actually have a receipt -- dividends are lumpy, not continuous) ---
    month_acc = defaultdict(lambda: defaultdict(int))
    for r in records:
        month_acc[r["month"]][r["account_type"]] += r["amount"]
    monthly = []
    for month in sorted(month_acc.keys()):
        buckets = month_acc[month]
        m_nisa = sum(v for k, v in buckets.items() if ACCOUNT_META[k]["taxable"] is False)
        m_taxable = sum(v for k, v in buckets.items() if ACCOUNT_META[k]["taxable"] is True)
        m_unknown = sum(v for k, v in buckets.items() if ACCOUNT_META[k]["taxable"] is None)
        monthly.append({"month": month, "nisa_total": m_nisa, "taxable_total": m_taxable, "unknown_total": m_unknown, "total": m_nisa + m_taxable + m_unknown})

    # --- yearly totals (fill every year in the contiguous range so no year is silently dropped) ---
    years_present = sorted({r["year"] for r in records}) or [as_of_year]
    first_year, last_year = years_present[0], max(years_present[-1], as_of_year)
    year_acc = defaultdict(lambda: defaultdict(int))
    year_sec = defaultdict(set)
    for r in records:
        year_acc[r["year"]][r["account_type"]] += r["amount"]
        year_sec[r["year"]].add(r["name"])
    yearly = []
    for y in range(first_year, last_year + 1):
        buckets = year_acc.get(y, {})
        y_nisa = sum(v for k, v in buckets.items() if ACCOUNT_META[k]["taxable"] is False)
        y_taxable = sum(v for k, v in buckets.items() if ACCOUNT_META[k]["taxable"] is True)
        y_unknown = sum(v for k, v in buckets.items() if ACCOUNT_META[k]["taxable"] is None)
        yearly.append(
            {
                "year": y,
                "nisa_total": y_nisa,
                "taxable_total": y_taxable,
                "unknown_total": y_unknown,
                "total": y_nisa + y_taxable + y_unknown,
                "security_count": len(year_sec.get(y, set())),
            }
        )

    # --- year x account-type matrix (keeps every year visible even if some buckets are 0) ---
    matrix_account_types = [k for k in ACCOUNT_ORDER if acc_counts[k] > 0]
    matrix_values = [[year_acc.get(y, {}).get(k, 0) for k in matrix_account_types] for y in range(first_year, last_year + 1)]

    # --- broker summary ---
    brokers = sorted({r["broker"] for r in records})
    brokers_summary = []
    for broker in brokers:
        b_records = [r for r in records if r["broker"] == broker]
        b_dates = sorted(r["date"] for r in b_records)
        brokers_summary.append(
            {
                "broker": broker,
                "date_range": [b_dates[0], b_dates[-1]] if b_dates else ["", ""],
                "total": sum(r["amount"] for r in b_records),
                "record_count": len(b_records),
                "security_count": len({r["name"] for r in b_records}),
            }
        )

    as_of_year_total = next((y["total"] for y in yearly if y["year"] == as_of_year), 0)
    prev_year = as_of_year - 1
    prev_year_row = next((y for y in yearly if y["year"] == prev_year), None)
    prev_year_total = prev_year_row["total"] if prev_year_row else None
    yoy_change_pct = None
    if prev_year_total:
        yoy_change_pct = round((as_of_year_total - prev_year_total) / prev_year_total * 100, 1)

    dates = sorted(r["date"] for r in records)
    detail_records = sorted(
        (
            {
                "date": r["date"],
                "broker": r["broker"],
                "code": r["code"],
                "name": r["name"],
                "account_type": r["account_type"],
                "account_label": ACCOUNT_META[r["account_type"]]["label"],
                "amount": r["amount"],
            }
            for r in records
        ),
        key=lambda r: r["date"],
    )

    return {
        "as_of_year": as_of_year,
        "totals": {
            "grand_total": grand_total,
            "record_count": len(records),
            "security_count": len(sec_map),
            "broker_count": len(brokers),
            "brokers": brokers,
            "date_range": [dates[0], dates[-1]] if dates else ["", ""],
            "nisa_total": nisa_total,
            "taxable_total": taxable_total,
            "unknown_total": unknown_total,
            "nisa_pct": pct(nisa_total, grand_total),
            "taxable_pct": pct(taxable_total, grand_total),
            "unknown_pct": pct(unknown_total, grand_total),
            "as_of_year_total": as_of_year_total,
            "prev_year": prev_year,
            "prev_year_total": prev_year_total,
            "yoy_change_pct": yoy_change_pct,
        },
        "monthly": monthly,
        "yearly": yearly,
        "securities": securities,
        "account_breakdown": account_breakdown,
        "year_account_matrix": {
            "years": list(range(first_year, last_year + 1)),
            "account_types": matrix_account_types,
            "values": matrix_values,
        },
        "brokers_summary": brokers_summary,
        "records": detail_records,
        "account_type_meta": ACCOUNT_META,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="JSON file listing input CSVs, one entry per broker")
    ap.add_argument("--out", default="report.html", help="Output HTML report path")
    ap.add_argument("--template", default="", help="Optional template.html path (default: alongside this script)")
    args = ap.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or not config.get("sources"):
        raise SystemExit("--config must be a JSON object with a non-empty \"sources\" array")

    base_dir = config_path.parent
    all_records = []
    total_skipped_blank = 0
    all_unknown_labels = Counter()
    for entry in config["sources"]:
        records, skipped_blank, unknown_labels = load_source_csv(entry, base_dir)
        label = entry.get("broker") or entry.get("csv")
        print(f"{label}: {len(records)} dividend/distribution rows ({skipped_blank} blank rows skipped)")
        all_records.extend(records)
        total_skipped_blank += skipped_blank
        all_unknown_labels.update(unknown_labels)

    if not all_records:
        raise SystemExit("no dividend/distribution rows loaded (check --config)")

    if all_unknown_labels:
        labels_str = ", ".join(f"{label!r}×{count}" for label, count in all_unknown_labels.most_common())
        print(
            f"WARNING: {sum(all_unknown_labels.values())} row(s) had an unrecognized account-type label "
            f"and were counted under 「口座区分 不明」: {labels_str}\n"
            f"  -> add the exact label to NISA_GROWTH_LABELS / NISA_TSUMITATE_LABELS / NISA_GENERAL_LABELS / "
            f"SPECIFIC_LABELS / GENERAL_LABELS in dividend_cashflow_tracker.py if this should be classified."
        )

    as_of_year = config.get("as_of_year") or max(r["year"] for r in all_records)
    data = compute_report_data(all_records, as_of_year)
    t = data["totals"]
    print(f"as_of_year={as_of_year}  total blank rows skipped: {total_skipped_blank}")
    print(
        f"受取総額(全期間): {t['grand_total']:,}円  |  銘柄数: {t['security_count']}  |  "
        f"NISA分(非課税): {t['nisa_total']:,}円({t['nisa_pct']}%)  |  課税口座分: {t['taxable_total']:,}円({t['taxable_pct']}%)"
    )
    print(f"{as_of_year}年 受取総額: {t['as_of_year_total']:,}円" + (f"（前年比 {t['yoy_change_pct']:+.1f}%）" if t["yoy_change_pct"] is not None else ""))

    template_path = Path(args.template) if args.template else Path(__file__).resolve().with_name("template.html")
    template = template_path.read_text(encoding="utf-8")
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    final_html = template.replace("__DATA_JSON__", data_json)

    out_path = Path(args.out)
    out_path.write_text(final_html, encoding="utf-8")
    print(f"written to {out_path} ({len(final_html)} chars)")


if __name__ == "__main__":
    main()
