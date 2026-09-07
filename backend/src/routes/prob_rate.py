"""예상 매칭확률 비율표 조회·갱신.

- POST /api/prob-rate/refresh — 리플리카 정착 코호트로 표를 다시 계산해 PG 에 기록.
  Cloud Scheduler 가 매일 1회 호출한다. 인증은 auto_dispatch 와 동일
  (X-Trigger-Secret 또는 세션).
- GET  /api/prob-rate — 현재 표. 플래너·운영자가 "이 확률의 근거 표본이 몇 건인지"를
  확인할 수 있어야 해서 n·raw_rate 를 같이 노출한다.

갱신 주기가 중요하다. 2026-07-02 자동 디스패치 V0 100% 승격 직후
`재이용·응답0` 셀의 실제 매칭률이 12.97% → 19.23% 로 뛰었는데, 학습창을 6개월로
고정하면 이런 개입을 몇 달 동안 못 따라간다. 롤링 90일 + 일 1회 갱신이 기본값인 이유.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from src.config import settings
from src.db import get_replica
from src.prob_rate_store import (
    build_cells,
    get_prob_rate_store,
    prob_rate_available,
)
from src.routes.auto_dispatch import trigger_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/prob-rate", tags=["prob-rate"])


@router.post("/refresh")
async def refresh(user: dict = Depends(trigger_auth)) -> dict[str, Any]:
    if not prob_rate_available():
        return {"status": "skipped", "reason": "MATCHING_OPS_DB_URL 미설정"}

    window = settings.prob_rate_window_days
    settle = settings.prob_rate_settle_days
    raw = await get_replica().prob_rate_cohort(window_days=window, settle_days=settle)
    if not raw:
        # 표를 비우지 않는다 — 코호트가 비면 직전 표를 그대로 두는 편이 안전하다.
        logger.warning("prob_rate 코호트가 비어 표를 갱신하지 않음 window=%d settle=%d",
                       window, settle)
        return {"status": "skipped", "reason": "empty_cohort",
                "window_days": window, "settle_days": settle}

    cells = build_cells(raw)
    written = await get_prob_rate_store().replace_all(cells, window)
    total_n = sum(c["n"] for c in cells if c["reuse"] == -1)
    total_m = sum(c["matched"] for c in cells if c["reuse"] == -1)
    thin = [f'{c["seg"]}/{c["age_bucket"]}/{c["reuse"]}{c["responder"]}'
            for c in cells if c["reuse"] != -1 and c["n"] < 100]
    logger.info("prob_rate refreshed cells=%d cohort_n=%d matched=%d thin=%d",
                written, total_n, total_m, len(thin))
    return {"status": "ok", "cells": written, "window_days": window,
            "settle_days": settle, "cohort_n": total_n, "cohort_matched": total_m,
            "thin_cells": thin}


@router.get("")
async def get_table(user: dict = Depends(trigger_auth)) -> dict[str, Any]:
    if not prob_rate_available():
        return {"status": "skipped", "reason": "MATCHING_OPS_DB_URL 미설정"}
    table = await get_prob_rate_store().load()
    rows = [
        {"seg": k[0], "age_bucket": k[1], "reuse": k[2], "responder": k[3],
         "rate": v[0], "n": v[1]}
        for k, v in sorted(table.items())
    ]
    return {"status": "ok", "count": len(rows), "rows": rows}
