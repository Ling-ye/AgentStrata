from __future__ import annotations

import io
from datetime import date, timedelta
from decimal import Decimal

import pytest

from chatcopilot.evals.capability_verifiers import judge_capability_trial
from chatcopilot.evals.fx_oracle import fetch_latest_usd_cny
from chatcopilot.evals.manifest import load_case_definitions
from chatcopilot.evals.models import TrialObservation
from chatcopilot.evals.registry import get_manifest


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _payload(period: str, *, usd: str = "1.2000", cny: str = "7.8000") -> bytes:
    return (
        "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
        f"EXR.D.CNY.EUR.SP00.A,D,CNY,EUR,SP00,A,{period},{cny}\n"
        f"EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,{period},{usd}\n"
    ).encode()


def test_fx_oracle_derives_usd_cny_from_same_day_ecb_observations(monkeypatch) -> None:
    monkeypatch.setattr(
        "chatcopilot.evals.fx_oracle.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(_payload(date.today().isoformat())),
    )

    reference = fetch_latest_usd_cny()

    assert reference.rate_date == date.today().isoformat()
    assert reference.usd_cny == Decimal("6.5")
    assert reference.to_evidence()["independent_from_agent_search"] is True


def test_fx_oracle_rejects_stale_observations(monkeypatch) -> None:
    stale = (date.today() - timedelta(days=8)).isoformat()
    monkeypatch.setattr(
        "chatcopilot.evals.fx_oracle.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(_payload(stale)),
    )

    with pytest.raises(ValueError, match="stale"):
        fetch_latest_usd_cny()


def test_current_fx_verifier_requires_date_source_value_and_independent_oracle() -> None:
    definition = next(
        item
        for item in load_case_definitions(get_manifest("agentstrata-capabilities-v1"))
        if item.case_id == "current-usd-cny-reference"
    )
    observation = TrialObservation(
        final_text=(
            "ECB 最新业务日参考：2026-08-21，1 USD = 6.7205 CNY。"
            "来源：https://data.ecb.europa.eu/"
        ),
        stop_reason="end_turn",
        evidence=(
            {
                "kind": "fx_reference",
                "source": "ECB Data Portal EXR",
                "rate_date": "2026-08-21",
                "base": "USD",
                "quote": "CNY",
                "rate": "6.720513",
                "independent_from_agent_search": True,
            },
                {
                    "kind": "search_trace",
                    "tool_event_ok": True,
                    "coordinator_ok": True,
                    "final_source_reference_count": 1,
                    "search_call_count": 1,
                    "requested_source_hints": ["web"],
                    "source_constraint_preserved": True,
                },
        ),
    )

    judge, _evidence = judge_capability_trial(definition, observation)

    assert judge.passed is True
