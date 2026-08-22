"""Independent ECB reference-rate oracle for the current-information Case."""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation


_ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/"
    "D.USD+CNY.EUR.SP00.A?lastNObservations=1&detail=dataonly&format=csvdata"
)


@dataclass(frozen=True)
class FxReference:
    rate_date: str
    usd_cny: Decimal
    fetched_at: str
    source: str = "ECB Data Portal EXR"

    def to_evidence(self) -> dict[str, object]:
        return {
            "kind": "fx_reference",
            "source": self.source,
            "rate_date": self.rate_date,
            "base": "USD",
            "quote": "CNY",
            "rate": str(self.usd_cny.quantize(Decimal("0.000001"))),
            "fetched_at": self.fetched_at,
            "independent_from_agent_search": True,
        }


def fetch_latest_usd_cny(*, timeout_seconds: float = 10.0) -> FxReference:
    """Fetch the latest common ECB business-day observations and derive USD/CNY."""

    request = urllib.request.Request(
        _ECB_URL,
        headers={"Accept": "text/csv", "User-Agent": "AgentStrata-Evaluation/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read(128 * 1024)
        if response.read(1):
            raise ValueError("ECB response exceeds the evaluation limit")
    text = payload.decode("utf-8-sig")
    rows = tuple(csv.DictReader(io.StringIO(text)))
    values: dict[str, tuple[str, Decimal]] = {}
    for row in rows:
        currency = str(row.get("CURRENCY") or "").strip()
        period = str(row.get("TIME_PERIOD") or "").strip()
        raw_value = str(row.get("OBS_VALUE") or "").strip()
        if currency not in {"USD", "CNY"} or not period or not raw_value:
            continue
        try:
            parsed_date = date.fromisoformat(period)
            value = Decimal(raw_value)
        except (ValueError, InvalidOperation) as exc:
            raise ValueError("ECB response contains an invalid observation") from exc
        if value <= 0 or parsed_date > date.today():
            raise ValueError("ECB response contains an invalid observation")
        values[currency] = (period, value)
    if set(values) != {"USD", "CNY"}:
        raise ValueError("ECB response does not contain USD and CNY observations")
    usd_date, usd_per_eur = values["USD"]
    cny_date, cny_per_eur = values["CNY"]
    if usd_date != cny_date:
        raise ValueError("ECB USD and CNY observations use different dates")
    if (date.today() - date.fromisoformat(usd_date)).days > 7:
        raise ValueError("ECB reference observation is stale")
    usd_cny = cny_per_eur / usd_per_eur
    if not Decimal("4") <= usd_cny <= Decimal("10"):
        raise ValueError("derived USD/CNY reference is outside the safety range")
    return FxReference(
        rate_date=usd_date,
        usd_cny=usd_cny,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def reference_fingerprint(reference: FxReference) -> str:
    """Return stable, non-secret canonical material for tests and diagnostics."""

    return json.dumps(reference.to_evidence(), ensure_ascii=True, sort_keys=True)


__all__ = ["FxReference", "fetch_latest_usd_cny", "reference_fingerprint"]
