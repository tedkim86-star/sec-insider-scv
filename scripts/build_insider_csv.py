import csv
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parents[1]
TICKERS_FILE = ROOT / "tickers.txt"
OUTPUT_FILE = ROOT / "data" / "insider_raw_data.csv"
TICKER_CONFIG_FILE = ROOT / "data" / "ticker_config.csv"

USER_AGENT = (
    os.environ.get("SEC_USER_AGENT")
    or os.environ.get("USER_AGENT")
    or "isispat-insider-tracker bjkim@isispat.com"
)
SINCE_DATE = os.environ.get("SINCE_DATE", "2026-05-26")
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "0.25"))

RAW_HEADERS = [
    "unique_key",
    "collected_at_utc",
    "ticker",
    "company_name",
    "cik",
    "form",
    "filing_date",
    "report_date",
    "accession_number",
    "filing_url",
    "transaction_table",
    "transaction_date",
    "transaction_code",
    "security_title",
    "shares",
    "price",
    "total_amount",
    "shares_owned_after",
    "ownership_type",
    "reporting_owner",
    "relationship",
    "is_director",
    "is_officer",
    "is_ten_percent_owner",
    "officer_title",
    "source",
]

CONFIG_HEADERS = [
    "ticker",
    "cik",
    "company_name",
    "since_date",
    "enabled",
    "status",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json, application/xml, text/xml, text/html, */*",
})


def normalize_ticker(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-")


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def fetch_text(url: str) -> str:
    response = SESSION.get(url, timeout=60)
    if response.status_code == 429:
        time.sleep(10)
        response = SESSION.get(url, timeout=60)
    response.raise_for_status()
    return response.text


def fetch_json(url: str):
    response = SESSION.get(url, timeout=60)
    if response.status_code == 429:
        time.sleep(10)
        response = SESSION.get(url, timeout=60)
    response.raise_for_status()
    return response.json()


def load_tickers() -> list[str]:
    if not TICKERS_FILE.exists():
        raise FileNotFoundError(f"Missing {TICKERS_FILE}")

    tickers = []
    text = TICKERS_FILE.read_text(encoding="utf-8-sig")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # 줄마다 하나씩 쓰는 것을 기본으로 하되,
        # AAPL, TSLA, NVDA처럼 쉼표로 적어도 처리함.
        parts = re.split(r"[,\s]+", line)
        for part in parts:
            part = part.strip()
            if part and not part.startswith("#"):
                tickers.append(normalize_ticker(part))

    return list(dict.fromkeys(tickers))


def load_sec_ticker_map() -> dict[str, dict]:
    url = "https://www.sec.gov/files/company_tickers.json"
    data = fetch_json(url)

    result = {}
    for item in data.values():
        ticker = normalize_ticker(item.get("ticker", ""))
        if not ticker:
            continue

        result[ticker] = {
            "cik": str(item["cik_str"]).zfill(10),
            "company_name": item.get("title", ""),
        }

    return result


def text_at(node: ET.Element | None, path: list[str]) -> str:
    cur = node
    for part in path:
        if cur is None:
            return ""
        cur = cur.find(part)

    return (cur.text or "").strip() if cur is not None else ""


def strip_default_namespace(xml_text: str) -> str:
    return re.sub(r'\sxmlns="[^"]+"', "", xml_text, count=1)


def extract_ownership_xml(document_text: str) -> str:
    text = document_text.strip()

    # SEC HTML/SGML 문서 안에 <XML>...</XML> 블록으로 들어 있는 경우 처리
    match = re.search(r"<XML>\s*(.*?)\s*</XML>", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        text = match.group(1).strip()

    # 앞쪽에 쓰레기 텍스트가 섞인 경우 ownershipDocument부터 잘라냄
    idx = text.find("<ownershipDocument")
    if idx >= 0:
        text = text[idx:]

    # 뒤쪽에 다른 문서가 붙은 경우 ownershipDocument 끝까지만 사용
    end_tag = "</ownershipDocument>"
    end_idx = text.find(end_tag)
    if end_idx >= 0:
        text = text[:end_idx + len(end_tag)]

    return text.strip()


def parse_owner(owner_node: ET.Element) -> dict:
    name = text_at(owner_node, ["reportingOwnerId", "rptOwnerName"])
    rel = owner_node.find("reportingOwnerRelationship")

    is_director = text_at(rel, ["isDirector"])
    is_officer = text_at(rel, ["isOfficer"])
    is_ten_percent_owner = text_at(rel, ["isTenPercentOwner"])
    officer_title = text_at(rel, ["officerTitle"])

    parts = []
    if is_director == "1":
        parts.append("Director")
    if is_officer == "1":
        parts.append("Officer")
    if is_ten_percent_owner == "1":
        parts.append("10% Owner")
    if officer_title:
        parts.append(officer_title)

    return {
        "reporting_owner": name,
        "relationship": ", ".join(parts),
        "is_director": is_director,
        "is_officer": is_officer,
        "is_ten_percent_owner": is_ten_percent_owner,
        "officer_title": officer_title,
    }


def empty_owner() -> dict:
    return {
        "reporting_owner": "",
        "relationship": "",
        "is_director": "",
        "is_officer": "",
        "is_ten_percent_owner": "",
        "officer_title": "",
    }


def to_float(value: str):
    if value is None or value == "":
        return None

    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def parse_transaction(node: ET.Element, table_type: str, owner: dict) -> dict:
    shares = text_at(node, [
        "transactionAmounts",
        "transactionShares",
        "value",
    ])

    price = text_at(node, [
        "transactionAmounts",
        "transactionPricePerShare",
        "value",
    ])

    shares_num = to_float(shares)
    price_num = to_float(price)

    total_amount = ""
    if shares_num is not None and price_num is not None:
        total_amount = shares_num * price_num

    return {
        "transaction_table": table_type,
        "transaction_date": text_at(node, [
            "transactionDate",
            "value",
        ]),
        "transaction_code": text_at(node, [
            "transactionCoding",
            "transactionCode",
        ]),
        "security_title": text_at(node, [
            "securityTitle",
            "value",
        ]),
        "shares": shares,
        "price": price,
        "total_amount": total_amount,
        "shares_owned_after": text_at(node, [
            "postTransactionAmounts",
            "sharesOwnedFollowingTransaction",
            "value",
        ]),
        "ownership_type": text_at(node, [
            "ownershipNature",
            "directOrIndirectOwnership",
            "value",
        ]),
        **owner,
    }


def parse_form4_document(document_text: str) -> list[dict]:
    xml_text = extract_ownership_xml(document_text)
    xml_text = strip_default_namespace(xml_text)

    root = ET.fromstring(xml_text.encode("utf-8"))

    if root.tag != "ownershipDocument":
        raise ValueError(f"Unexpected root tag: {root.tag}")

    owners = [parse_owner(node) for node in root.findall("reportingOwner")]
    owner = owners[0] if owners else empty_owner()

    rows = []

    non_derivative = root.find("nonDerivativeTable")
    if non_derivative is not None:
        for tx in non_derivative.findall("nonDerivativeTransaction"):
            rows.append(parse_transaction(tx, "nonDerivative", owner))

    derivative = root.find("derivativeTable")
    if derivative is not None:
        for tx in derivative.findall("derivativeTransaction"):
            rows.append(parse_transaction(tx, "derivative", owner))

    return rows


def extract_form4_filings(submissions: dict, since: date) -> list[dict]:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])

    out = []
    for idx, form in enumerate(forms):
        if form not in {"4", "4/A"}:
            continue

        filing_date = recent.get("filingDate", [])[idx]
        if parse_date(filing_date) < since:
            continue

        out.append({
            "form": form,
            "filing_date": filing_date,
            "report_date": recent.get("reportDate", [""] * len(forms))[idx] or "",
            "accession_number": recent.get("accessionNumber", [])[idx],
            "primary_document": recent.get("primaryDocument", [""] * len(forms))[idx] or "",
        })

    return out


def candidate_document_urls(cik_no_zeros: str, accession_number: str, primary_document: str) -> list[str]:
    accession_no_dash = accession_number.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/{accession_no_dash}"

    urls = []

    if primary_document:
        urls.append(f"{base}/{primary_document}")

    # accession index에서 실제 XML 후보를 추가 탐색
    index_url = f"{base}/index.json"
    try:
        index = fetch_json(index_url)
        items = index.get("directory", {}).get("item", [])
        for item in items:
            name = item.get("name", "")
            if not name:
                continue

            lower = name.lower()
            if lower.endswith(".xml"):
                urls.append(f"{base}/{name}")

            # 일부 Form 4는 doc4.xml, ownership.xml 등으로 저장됨
            if "ownership" in lower or "form4" in lower:
                urls.append(f"{base}/{name}")
    except Exception as exc:
        print(f"WARN: failed to fetch filing index {index_url}: {exc}", file=sys.stderr)

    # 중복 제거
    return list(dict.fromkeys(urls))


def build_rows() -> tuple[list[dict], list[dict]]:
    since = parse_date(SINCE_DATE)
    tickers = load_tickers()

    print(f"Loaded {len(tickers)} tickers: {', '.join(tickers[:20])}{'...' if len(tickers) > 20 else ''}")
    print(f"Using SINCE_DATE={SINCE_DATE}")
    print(f"Using USER_AGENT={USER_AGENT}")

    ticker_map = load_sec_ticker_map()

    rows = []
    config_rows = []
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")

    total_filings = 0
    total_transactions = 0

    for ticker in tickers:
        info = ticker_map.get(ticker)

        if not info:
            config_rows.append({
                "ticker": ticker,
                "cik": "",
                "company_name": "",
                "since_date": SINCE_DATE,
                "enabled": "FALSE",
                "status": "NOT_FOUND",
            })
            print(f"WARN: ticker not found: {ticker}", file=sys.stderr)
            continue

        cik = info["cik"]
        cik_no_zeros = str(int(cik))
        company_name = info["company_name"]

        config_rows.append({
            "ticker": ticker,
            "cik": cik,
            "company_name": company_name,
            "since_date": SINCE_DATE,
            "enabled": "TRUE",
            "status": "OK",
        })

        submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        submissions = fetch_json(submissions_url)
        filings = extract_form4_filings(submissions, since)

        print(f"{ticker}: {len(filings)} Form 4/4-A filings since {SINCE_DATE}")
        total_filings += len(filings)

        ticker_transaction_count = 0

        for filing in filings:
            time.sleep(REQUEST_DELAY_SECONDS)

            urls = candidate_document_urls(
                cik_no_zeros=cik_no_zeros,
                accession_number=filing["accession_number"],
                primary_document=filing["primary_document"],
            )

            transactions = []
            used_url = ""

            for filing_url in urls:
                try:
                    document_text = fetch_text(filing_url)
                    transactions = parse_form4_document(document_text)
                    used_url = filing_url
                    break
                except Exception as exc:
                    print(f"WARN: failed parse candidate {ticker} {filing_url}: {exc}", file=sys.stderr)
                    continue

            if not transactions:
                print(
                    f"WARN: no transactions parsed for {ticker} {filing['accession_number']}",
                    file=sys.stderr,
                )
                continue

            ticker_transaction_count += len(transactions)
            total_transactions += len(transactions)

            for tx in transactions:
                unique_key = "|".join([
                    ticker,
                    filing["accession_number"],
                    tx["transaction_table"],
                    tx["transaction_date"],
                    tx["reporting_owner"],
                    tx["transaction_code"],
                    tx["security_title"],
                    str(tx["shares"]),
                    str(tx["price"]),
                    tx["ownership_type"],
                ])

                rows.append({
                    "unique_key": unique_key,
                    "collected_at_utc": collected_at,
                    "ticker": ticker,
                    "company_name": company_name,
                    "cik": cik,
                    "form": filing["form"],
                    "filing_date": filing["filing_date"],
                    "report_date": filing["report_date"],
                    "accession_number": filing["accession_number"],
                    "filing_url": used_url,
                    "source": "SEC EDGAR",
                    **tx,
                })

        print(f"{ticker}: parsed {ticker_transaction_count} transaction rows")

    deduped = {row["unique_key"]: row for row in rows}

    rows = sorted(
        deduped.values(),
        key=lambda r: (
            r.get("filing_date", ""),
            r.get("ticker", ""),
            r.get("reporting_owner", ""),
        ),
        reverse=True,
    )

    print(f"Total Form 4/4-A filings found: {total_filings}")
    print(f"Total transaction rows parsed before dedupe: {total_transactions}")
    print(f"Total transaction rows after dedupe: {len(rows)}")

    return rows, config_rows


def write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rows, config_rows = build_rows()

    write_csv(OUTPUT_FILE, RAW_HEADERS, rows)
    write_csv(TICKER_CONFIG_FILE, CONFIG_HEADERS, config_rows)

    print(f"Wrote {len(rows)} rows to {OUTPUT_FILE}")
    print(f"Wrote {len(config_rows)} rows to {TICKER_CONFIG_FILE}")


if __name__ == "__main__":
    main()
