"""CAL format parser for California e-filing data.

Parses the CAL (California e-filing) text format extracted from
NetFile ZIP downloads into structured dicts ready for Transaction
model creation.

CAL format: comma-separated records, one per line, with a record-type
prefix. Transaction-bearing record types:
  RCPT — contributions (Schedule A, C)
  EXPN — expenditures (Schedule E)
  LOAN — loans received (Schedule B1)
  DEBT — accrued expenses / debts (Schedule F)

Non-transaction records (HDR, CVR, CVR2, CVR3, SMRY, SPLT, TEXT)
are skipped for transaction extraction but CVR is parsed for
filer metadata.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date

logger = logging.getLogger(__name__)

# CAL field positions for each transaction record type.
# These are 0-indexed positions in the comma-separated line.
# Derived from actual CSHA filing samples.

# RCPT: record_type, schedule, txn_id, entity_type, last, first, ...unused...,
#        city, state, zip, employer, occupation, ...unused..., memo_flag,
#        date, ...unused, amount, cumulative, ...
_RCPT_FIELDS = {
    "schedule": 1,
    "txn_id": 2,
    "entity_type": 3,
    "last_name": 4,
    "first_name": 5,
    "city": 10,
    "state": 11,
    "zip_code": 12,
    "employer": 13,
    "occupation": 14,
    "memo_code": 16,
    "date": 17,
    "amount": 19,
    "cumulative": 20,
}

# EXPN: record_type, schedule, txn_id, entity_type, last, first, ...unused...,
#        city, state, zip, date, amount, cumulative, ...unused,
#        expense_code, description
_EXPN_FIELDS = {
    "schedule": 1,
    "txn_id": 2,
    "entity_type": 3,
    "last_name": 4,
    "first_name": 5,
    "city": 10,
    "state": 11,
    "zip_code": 12,
    "date": 13,
    "amount": 14,
    "cumulative": 15,
    "expense_code": 17,
    "description": 18,
}

# LOAN: record_type, schedule, txn_id, ...unused, entity_type, last, first,
#        ...unused..., city, state, zip, loan_date, due_date, ...amounts...
_LOAN_FIELDS = {
    "schedule": 1,
    "txn_id": 2,
    "entity_type": 4,
    "last_name": 5,
    "first_name": 6,
    "city": 11,
    "state": 12,
    "zip_code": 13,
    "date": 14,           # loan_date_1 (origination)
    "amount": 18,         # outstanding_balance
}

# DEBT: record_type, schedule, txn_id, entity_type, last, first, ...unused...,
#        city, state, zip, amount_outstanding_beg, amount_this_period,
#        amount_outstanding_end, ...
_DEBT_FIELDS = {
    "schedule": 1,
    "txn_id": 2,
    "entity_type": 3,
    "last_name": 4,
    "first_name": 5,
    "city": 10,
    "state": 11,
    "zip_code": 12,
    "amount": 14,         # amount_incurred (this period)
}

# Map record type to field layout and transaction_type
_RECORD_CONFIGS = {
    "RCPT": (_RCPT_FIELDS, "contribution"),
    "EXPN": (_EXPN_FIELDS, "expenditure"),
    "LOAN": (_LOAN_FIELDS, "loan"),
    "DEBT": (_DEBT_FIELDS, "debt"),
}

# Schedule letter to human-readable label
SCHEDULE_LABELS = {
    "A": "Monetary Contributions",
    "B1": "Loans Received",
    "C": "Nonmonetary Contributions",
    "E": "Payments Made",
    "F": "Accrued Expenses",
}


def _safe_float(val: str) -> float | None:
    """Parse a float, returning None for empty/invalid."""
    if not val or not val.strip():
        return None
    try:
        return float(val.strip())
    except ValueError:
        return None


def _safe_date(val: str) -> date | None:
    """Parse a YYYYMMDD date string."""
    val = (val or "").strip()
    if not val or len(val) != 8:
        return None
    try:
        return date(int(val[:4]), int(val[4:6]), int(val[6:8]))
    except (ValueError, IndexError):
        return None


def _get_field(fields: list[str], idx: int) -> str:
    """Safely get a field by index, returning empty string if out of range."""
    if idx < len(fields):
        return fields[idx].strip()
    return ""


def _build_entity_name(last: str, first: str) -> str:
    """Build display name from last/first name fields."""
    if first and last:
        return f"{first} {last}"
    return last or first or ""


def parse_cal_lines(cal_text: str) -> list[list[str]]:
    """Parse CAL text into a list of field arrays, one per line.

    Uses csv.reader to handle any quoted fields.
    """
    lines = []
    reader = csv.reader(io.StringIO(cal_text))
    for row in reader:
        if row:
            lines.append(row)
    return lines


def parse_cal_transactions(cal_text: str) -> list[dict]:
    """Parse CAL format text and return normalized transaction dicts.

    Each dict is ready for Transaction model creation with fields:
    schedule, transaction_type, transaction_type_code, entity_name,
    entity_type, first_name, last_name, city, state, zip_code,
    employer, occupation, amount, cumulative_amount, transaction_date,
    description, memo_code, netfile_transaction_id, data_source
    """
    lines = parse_cal_lines(cal_text)
    transactions = []

    for fields in lines:
        record_type = fields[0].strip() if fields else ""

        if record_type not in _RECORD_CONFIGS:
            continue

        field_map, txn_type = _RECORD_CONFIGS[record_type]

        last_name = _get_field(fields, field_map.get("last_name", -1))
        first_name = _get_field(fields, field_map.get("first_name", -1))
        amount = _safe_float(_get_field(fields, field_map.get("amount", -1)))

        # Skip records with no amount
        if amount is None:
            continue

        schedule = _get_field(fields, field_map.get("schedule", -1))
        txn_id = _get_field(fields, field_map.get("txn_id", -1))

        memo_raw = _get_field(fields, field_map.get("memo_code", -1))
        is_memo = memo_raw.upper() in ("X", "T", "TRUE", "F")  # F = forgiven loan in RCPT

        txn = {
            "schedule": schedule,
            "transaction_type": txn_type,
            "transaction_type_code": record_type,
            "entity_name": _build_entity_name(last_name, first_name),
            "entity_type": _get_field(fields, field_map.get("entity_type", -1)),
            "first_name": first_name,
            "last_name": last_name,
            "city": _get_field(fields, field_map.get("city", -1)),
            "state": _get_field(fields, field_map.get("state", -1)),
            "zip_code": _get_field(fields, field_map.get("zip_code", -1)),
            "employer": _get_field(fields, field_map.get("employer", -1)) or None,
            "occupation": _get_field(fields, field_map.get("occupation", -1)) or None,
            "amount": amount,
            "cumulative_amount": _safe_float(
                _get_field(fields, field_map.get("cumulative", -1))
            ),
            "transaction_date": _safe_date(
                _get_field(fields, field_map.get("date", -1))
            ),
            "description": _get_field(fields, field_map.get("description", -1)) or None,
            "memo_code": is_memo,
            "netfile_transaction_id": txn_id or None,
            "data_source": "efile",
        }
        transactions.append(txn)

    logger.info("Parsed %d transactions from CAL data", len(transactions))
    return transactions
