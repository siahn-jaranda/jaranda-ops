"""[수동관리 신청서] — 플래너가 우선순위 순서대로 처리하는 목록.

메뉴 2개. 우선순위는 필터가 아니라 **배경 정렬**이다 — 플래너는 위에서부터 처리한다.

- recommended — 선생님 추천 상태(20)가 된 지 48시간 지난 건. 오래 방치된 순.
- intake — 접수안내(10) 중 예상 매칭확률 **상위 30% / 하위 30% 분위**.
  상위는 밀어주면 닫히는 건, 하위는 구조적 개입이 필요한 건이라 성격이 다르다.

  🚨 절대값(80% 이상 / 20% 이하)이 아니라 **분위**인 이유 — 실측 분포에서
  80% 이상이 14건, 20% 이하가 1건으로 쏠려 화면이 비거나 넘친다.
  분위는 물량이 변해도 항상 처리 가능한 크기를 유지한다.

카드 스키마는 메인 목록과 동일하다(row_batch).
인증은 managed 와 동일한 trigger_auth.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from src.db import get_replica
from src.routes.auto_dispatch import trigger_auth
from src.row_batch import build_rows_for_sids

router = APIRouter(prefix="/api/manual-ops", tags=["manual-ops"])
logger = logging.getLogger(__name__)

MENUS = ("recommended", "intake")
# 분위 — 상위/하위 각 30%
QUANTILE = 0.30


def _prob(row: dict[str, Any]) -> float:
    p = row.get("prob") or {}
    try:
        return float(p.get("value") or 0)
    except (TypeError, ValueError):
        return 0.0


def _split_quantile(rows: list[dict[str, Any]]) -> tuple[list, list]:
    """확률 기준 상위 30% / 하위 30%. 중간 40% 는 어느 그룹도 아니다.

    표본이 너무 적어 두 그룹이 같은 건을 물면(<= 2k) 반으로 갈라 겹침을 없앤다.
    같은 신청서가 '밀어줄 건'과 '막힌 건'에 동시에 뜨면 화면이 거짓말을 한다.
    풀이 1건뿐이면 하위는 비는 게 맞다 — 한 건을 양쪽에 넣을 수는 없다.
    """
    if not rows:
        return [], []
    ordered = sorted(rows, key=_prob, reverse=True)
    k = max(1, int(round(len(ordered) * QUANTILE)))
    high = ordered[:k]
    low = ordered[-k:]
    if len(ordered) <= 2 * k:  # 표본이 적어 두 그룹이 겹치는 경우
        mid = len(ordered) // 2
        high, low = ordered[:mid or 1], ordered[mid or 1:]
    return high, list(reversed(low))  # 하위는 낮은 것부터


@router.get("")
async def list_manual_ops(
    menu: str = Query("recommended"),
    stale_hours: int = Query(48, ge=1, le=720),
    limit: int = Query(300, le=500),
    _user: dict = Depends(trigger_auth),
) -> dict[str, Any]:
    if menu not in MENUS:
        raise HTTPException(status_code=400, detail=f"menu 는 {MENUS} 중 하나")

    try:
        sids = await get_replica().list_manual_ops_sids(
            menu=menu, stale_hours=stale_hours, limit=limit
        )
    except Exception:
        logger.exception("manual_ops sid 조회 실패 menu=%s", menu)
        raise HTTPException(status_code=503, detail="replica query failed")

    if not sids:
        return {"menu": menu, "count": 0, "groups": [], "rows": []}

    row_map = await build_rows_for_sids(sids)
    # sid 조회 순서가 곧 우선순위다(recommended = 오래 방치된 순). 그 순서를 보존한다.
    rows = [row_map[s] for s in sids if s in row_map]

    if menu == "recommended":
        groups = [{
            "key": "stale",
            "label": f"{stale_hours}시간 경과",
            "hint": "선생님 추천 후 부모님 확정이 없는 건. 오래된 순.",
            "count": len(rows),
        }]
        logger.info("manual_ops menu=recommended stale_h=%d rows=%d", stale_hours, len(rows))
        return {"menu": menu, "count": len(rows), "groups": groups, "rows": rows}

    high, low = _split_quantile(rows)
    for r in high:
        r["opsGroup"] = "high"
    for r in low:
        r["opsGroup"] = "low"
    merged = high + low
    groups = [
        {"key": "high", "label": "매칭확률 상위 30%", "count": len(high),
         "hint": "밀어주면 닫히는 건. 확정 유도."},
        {"key": "low", "label": "매칭확률 하위 30%", "count": len(low),
         "hint": "구조적으로 막힌 건. 조건 조정·후보 확장 필요."},
    ]
    logger.info("manual_ops menu=intake total=%d high=%d low=%d", len(rows), len(high), len(low))
    return {"menu": menu, "count": len(merged), "groups": groups, "rows": merged,
            "poolSize": len(rows)}
