"""예상 매칭확률 비율표 — 셀별 과거 실측 매칭률.

셀 = (상품군 seg, 신청서 나이구간 age_bucket, 재이용 reuse, 응답 선생님 유무 responder).
서빙은 이 표를 조회만 한다. 축소(shrinkage)는 갱신 시점에 이미 반영돼 있다.

배경과 검증은 sql/0013_create_prob_rate.sql 및
옵시디언 `수동매칭 대시보드/matching-ops 예상 매칭확률 캘리브레이션.md` 참고.

표가 없거나 조회에 실패해도 서빙은 멈추지 않는다 — _DEFAULT_RATES 로 폴백한다.
이 기본값은 2026-02-01~08-07 실측(축소 적용 후)이며, 갱신 배치가 돌면 덮인다.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from src.config import settings

logger = logging.getLogger(__name__)

SEGS = ("reg2", "reg3", "urgent")
AGE_BUCKETS = ("lt6h", "6to48h", "gte48h")
# 구간별 대표 관측 시점(시간). 갱신 배치가 이 시점 스냅샷으로 비율을 만든다.
BUCKET_SNAPSHOT_H = {"lt6h": 3, "6to48h": 24, "gte48h": 72}
# 축소 강도. 셀 표본이 K 와 같아지면 상위 셀과 반반 섞인다.
SHRINK_K = 50
# 이보다 얇은 셀은 아예 상위 셀 값을 쓴다.
MIN_CELL_N = 30

_DEFAULT_RATES: dict[tuple[str, str, int, int], tuple[float, int]] = {
    ("reg2", "lt6h", 0, 0): (5.6, 3056),
    ("reg2", "lt6h", 0, 1): (12.4, 1366),
    ("reg2", "lt6h", 1, 0): (13.9, 2619),
    ("reg2", "lt6h", 1, 1): (23.2, 1691),
    ("reg2", "6to48h", 0, 0): (2.4, 1751),
    ("reg2", "6to48h", 0, 1): (7.1, 2210),
    ("reg2", "6to48h", 1, 0): (7.2, 1332),
    ("reg2", "6to48h", 1, 1): (12.9, 2243),
    ("reg2", "gte48h", 0, 0): (0.2, 1013),
    ("reg2", "gte48h", 0, 1): (0.2, 2207),
    ("reg2", "gte48h", 1, 0): (1.0, 711),
    ("reg2", "gte48h", 1, 1): (0.9, 1935),
    ("reg3", "lt6h", 0, 0): (12.3, 616),
    ("reg3", "lt6h", 0, 1): (21.8, 230),
    ("reg3", "lt6h", 1, 0): (25.9, 1048),
    ("reg3", "lt6h", 1, 1): (35.5, 508),
    ("reg3", "6to48h", 0, 0): (7.2, 364),
    ("reg3", "6to48h", 0, 1): (12.1, 337),
    ("reg3", "6to48h", 1, 0): (15.0, 533),
    ("reg3", "6to48h", 1, 1): (22.9, 583),
    ("reg3", "gte48h", 0, 0): (2.1, 208),
    ("reg3", "gte48h", 0, 1): (1.9, 298),
    ("reg3", "gte48h", 1, 0): (6.1, 255),
    ("reg3", "gte48h", 1, 1): (2.3, 411),
    ("urgent", "lt6h", 0, 0): (27.6, 63),
    ("urgent", "lt6h", 0, 1): (47.1, 78),
    ("urgent", "lt6h", 1, 0): (24.6, 179),
    ("urgent", "lt6h", 1, 1): (49.9, 399),
    ("urgent", "6to48h", 0, 0): (25.8, 13),
    ("urgent", "6to48h", 0, 1): (27.6, 27),
    ("urgent", "6to48h", 1, 0): (26.5, 30),
    ("urgent", "6to48h", 1, 1): (35.9, 104),
    ("urgent", "gte48h", 0, 0): (23.7, 4),
    ("urgent", "gte48h", 0, 1): (24.6, 6),
    ("urgent", "gte48h", 1, 0): (26.4, 6),
    ("urgent", "gte48h", 1, 1): (27.0, 27),
    ("reg2", "6to48h", -1, -1): (7.8, 7536),
    ("reg2", "gte48h", -1, -1): (0.5, 5866),
    ("reg2", "lt6h", -1, -1): (12.6, 8732),
    ("reg3", "6to48h", -1, -1): (15.3, 1817),
    ("reg3", "gte48h", -1, -1): (3.0, 1172),
    ("reg3", "lt6h", -1, -1): (24.0, 2402),
    ("urgent", "6to48h", -1, -1): (30.5, 174),
    ("urgent", "gte48h", -1, -1): (25.6, 43),
    ("urgent", "lt6h", -1, -1): (40.5, 719),
}


def age_bucket(age_minutes: float | None) -> str:
    """신청서 나이(분) → 구간. 나이를 모르면 가장 흔한 구간으로 둔다."""
    if age_minutes is None:
        return "6to48h"
    if age_minutes < 6 * 60:
        return "lt6h"
    if age_minutes < 48 * 60:
        return "6to48h"
    return "gte48h"


def segment(regularity: Any, is_urgent: Any) -> str:
    """상품군. 긴급이 최우선 — regularity=1 과 100% 겹치지만 명시적으로 가른다."""
    if is_urgent:
        return "urgent"
    try:
        if int(regularity) == 3:
            return "reg3"
    except (TypeError, ValueError):
        pass
    return "reg2"


class ProbRateStore:
    """비율표 저장소. 조회는 프로세스 캐시(TTL)를 태운다 — 목록 요청마다 PG 를 때리지 않게."""

    def __init__(self, cache_ttl_sec: int = 600) -> None:
        self._engine: AsyncEngine = create_async_engine(
            settings.matching_ops_db_url, pool_pre_ping=True, pool_size=2, max_overflow=2
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._cache: dict[tuple[str, str, int, int], tuple[float, int]] | None = None
        self._cache_at = 0.0
        self._ttl = cache_ttl_sec

    async def aclose(self) -> None:
        await self._engine.dispose()

    def invalidate(self) -> None:
        self._cache = None

    async def load(self) -> dict[tuple[str, str, int, int], tuple[float, int]]:
        """{(seg, bucket, reuse, responder): (rate, n)}. 실패하면 기본표."""
        now = time.monotonic()
        if self._cache is not None and now - self._cache_at < self._ttl:
            return self._cache
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(text(
                    "SELECT seg, age_bucket, reuse, responder, rate, n"
                    "  FROM matching_ops_prob_rate"
                ))).fetchall()
            table = {(r[0], r[1], int(r[2]), int(r[3])): (float(r[4]), int(r[5])) for r in rows}
            if not table:
                logger.info("prob_rate 표가 비어 있음 — 기본표 사용")
                table = dict(_DEFAULT_RATES)
        except Exception:
            logger.exception("prob_rate 조회 실패 — 기본표로 폴백 (graceful)")
            table = dict(_DEFAULT_RATES)
        self._cache, self._cache_at = table, now
        return table

    async def replace_all(self, cells: list[dict[str, Any]], window_days: int) -> int:
        """갱신 배치가 계산한 셀 전체를 upsert. 반환 = 기록한 행 수."""
        if not cells:
            return 0
        stmt = text(
            """
            INSERT INTO matching_ops_prob_rate
                (seg, age_bucket, reuse, responder, n, matched, rate, raw_rate,
                 window_days, computed_at)
            VALUES (:seg, :age_bucket, :reuse, :responder, :n, :matched, :rate, :raw_rate,
                    :window_days, NOW())
            ON CONFLICT (seg, age_bucket, reuse, responder) DO UPDATE SET
                n = EXCLUDED.n, matched = EXCLUDED.matched, rate = EXCLUDED.rate,
                raw_rate = EXCLUDED.raw_rate, window_days = EXCLUDED.window_days,
                computed_at = EXCLUDED.computed_at
            """
        )
        async with self._session_factory() as session:
            for c in cells:
                await session.execute(stmt, {**c, "window_days": window_days})
            await session.commit()
        self.invalidate()
        return len(cells)


def build_cells(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """리플리카 집계(seg, age_bucket, reuse, responder, n, matched) → 축소 적용 셀 목록.

    상위 셀(reuse=-1, responder=-1)을 먼저 만들고, 잎 셀을 그쪽으로 당긴다.
    표본이 MIN_CELL_N 미만이면 상위 셀 값을 그대로 쓴다(rate 만; n·matched 는 원값 보존).
    """
    parent: dict[tuple[str, str], list[int]] = {}
    for r in raw:
        key = (r["seg"], r["age_bucket"])
        acc = parent.setdefault(key, [0, 0])
        acc[0] += int(r["n"])
        acc[1] += int(r["matched"])

    out: list[dict[str, Any]] = []
    for key, (pn, pm) in parent.items():
        if pn <= 0:
            continue
        rate = round(100.0 * pm / pn, 2)
        out.append({"seg": key[0], "age_bucket": key[1], "reuse": -1, "responder": -1,
                    "n": pn, "matched": pm, "rate": rate, "raw_rate": rate})
    for r in raw:
        n, m = int(r["n"]), int(r["matched"])
        if n <= 0:
            continue
        pn, pm = parent[(r["seg"], r["age_bucket"])]
        p_rate = 100.0 * pm / pn
        raw_rate = 100.0 * m / n
        rate = p_rate if n < MIN_CELL_N else 100.0 * (m + SHRINK_K * p_rate / 100.0) / (n + SHRINK_K)
        out.append({"seg": r["seg"], "age_bucket": r["age_bucket"],
                    "reuse": int(r["reuse"]), "responder": int(r["responder"]),
                    "n": n, "matched": m,
                    "rate": round(rate, 2), "raw_rate": round(raw_rate, 2)})
    return out


_store: ProbRateStore | None = None


def get_prob_rate_store() -> ProbRateStore:
    global _store
    if _store is None:
        _store = ProbRateStore()
    return _store


def prob_rate_available() -> bool:
    return bool(settings.matching_ops_db_url)
