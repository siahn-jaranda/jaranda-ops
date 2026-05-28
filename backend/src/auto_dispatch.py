"""자동 디스패치 — 지원 0개 신청서에 LLM 추천 선생님 추가 + 방문 제안 발송.

흐름:
  1) 신청서 1차 필터 (replica: status=10, age≥1h, 지목 0명, 좌표 있음)
  2) PG 제외 (auto_run.live / memo / handler 어느 하나라도 있으면 skip)
  3) 일일 cap (live만): KST 오늘 처리한 신청서 수가 daily_max_apps 이상이면 중단
  4) 신청서별 처리:
     a. 후보 풀 (인접 시군구·활동중·과목 매칭) 50명 확보
     b. cooldown — 오늘 추천 알림 ≥ cap 받은 선생님 사전 제외
     c. LLM 랭킹 (AUTO_DISPATCH_SYSTEM_PROMPT, 상위 20명)
     d. live: console add_teachers → write_memo → send_visit_offers
        dry-run: 호출 안 함, 결과만 로그
     e. matching_ops_auto_run UPSERT (성공·실패 무관)
  5) 슬랙 요약 알림 (옵션) + 결과 dict 반환

신청서 1건 실패가 전체를 멈추지 않도록 per-recommendation try/except.
race: replica polling 기반이라 atomic하지 않음. console succeedCount=0 = 충돌 신호.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.auto_run_store import auto_run_available, get_auto_run_store
from src.config import settings
from src.console_client import ConsoleApiError, console_available, get_console_client
from src.db import get_replica
from src.llm_client import AUTO_DISPATCH_SYSTEM_PROMPT, get_llm_client
from src.llm_insight_store import get_llm_insight_store, llm_insight_available
from src.routes.candidates import _build_input, _candidate_view, _parse_schedule

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))

# 신청서 1건당 LLM 입력 후보 풀 상한. cooldown 제외 후 top_n 채울 여유.
_RAW_POOL_LIMIT = 50
# LLM 응답 max_tokens — 상위 20명 ranking은 RECOMMEND(5~7) 대비 더 필요.
# 한 item ~80 token × 20 + summary/note → 약 1700-2000. 안전하게 4096.
_LLM_MAX_TOKENS = 4096


class AutoDispatchUnavailable(RuntimeError):
    """필수 설정 누락 — 호출자가 503으로 변환."""


async def run_once(
    *,
    dry_run: bool,
    max_apps: int | None,
    operator_email: str,
) -> dict[str, Any]:
    """자동 디스패치 1회 실행. 결과 요약 dict 반환.

    max_apps=None이면 settings.auto_dispatch_daily_max_apps 사용.
    """
    started_at = datetime.now(KST)
    requested_cap = max_apps if max_apps is not None else settings.auto_dispatch_daily_max_apps

    if not auto_run_available():
        raise AutoDispatchUnavailable("MATCHING_OPS_DB_URL 미설정")
    if not llm_insight_available():
        raise AutoDispatchUnavailable("ANTHROPIC_API_KEY 미설정")
    if not dry_run and not console_available():
        raise AutoDispatchUnavailable("CONSOLE_USERNAME / CONSOLE_PASSWORD 미설정")

    replica = get_replica()
    store = get_auto_run_store()
    llm = get_llm_client()
    console = get_console_client()

    # 1) replica 1차 필터 (cap의 5배 확보 → PG 제외 후 cap 충분)
    raw = await replica.list_auto_dispatch_candidates(
        min_age_minutes=settings.auto_dispatch_min_age_minutes,
        limit=max(requested_cap * 5, 10),
    )
    sids_pre = [r["sid"] for r in raw]
    logger.info("auto_dispatch step1 raw_candidates=%d", len(sids_pre))

    # 2) PG 제외
    excluded = await store.get_excluded_sids(sids_pre)
    eligible = [r for r in raw if r["sid"] not in excluded]
    logger.info(
        "auto_dispatch step2 excluded=%d eligible=%d",
        len(excluded), len(eligible),
    )

    # 3) 일일 cap (live만)
    skipped_reason: str | None = None
    if not dry_run:
        today_live = await store.count_today_runs(dry_run=False)
        remaining = max(0, settings.auto_dispatch_daily_max_apps - today_live)
        effective_cap = min(requested_cap, remaining)
        if effective_cap <= 0:
            skipped_reason = (
                f"daily_cap_reached today_live={today_live} "
                f"max={settings.auto_dispatch_daily_max_apps}"
            )
            logger.warning("auto_dispatch %s", skipped_reason)
            targets = []
        else:
            targets = eligible[:effective_cap]
    else:
        targets = eligible[:requested_cap]

    # 4) 신청서별 처리
    processed: list[dict[str, Any]] = []
    for app in targets:
        sid = str(app["sid"])
        try:
            res = await _process_one(
                app, dry_run=dry_run, operator_email=operator_email,
                replica=replica, llm=llm, console=console, store=store,
            )
        except AutoDispatchUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("auto_dispatch process_one failed sid=%s", sid)
            try:
                await store.record_run(
                    recommendation_sid=sid,
                    dry_run=dry_run,
                    pool_size=0,
                    added_count=0,
                    succeed_count=0,
                    denied_count=0,
                    llm_model_id=settings.llm_recommend_model_id,
                    operator_email=operator_email,
                    error_message=str(e)[:1000],
                )
            except Exception:
                logger.exception("auto_dispatch record_run on error failed sid=%s", sid)
            res = {"sid": sid, "status": "error", "error": str(e)[:300]}
        processed.append(res)

    summary = _make_summary(
        dry_run=dry_run,
        requested_cap=requested_cap,
        effective_targets=len(targets),
        eligible=len(eligible),
        raw=len(raw),
        excluded=len(excluded),
        processed=processed,
        skipped_reason=skipped_reason,
        started_at=started_at,
        operator_email=operator_email,
    )

    # 5) 슬랙 (옵션)
    await _post_slack_summary(summary)

    return summary


async def _process_one(
    app: dict[str, Any],
    *,
    dry_run: bool,
    operator_email: str,
    replica,
    llm,
    console,
    store,
) -> dict[str, Any]:
    sid = str(app["sid"])
    spec = int(app.get("teacher_specialties") or 5)
    statuses = [2]  # 활동중만
    lat = float(app["lat"])
    lng = float(app["lng"])

    # a) 후보 풀
    gu_codes = await replica.find_nearby_sigungu(lat, lng, 3)
    cands = await replica.list_candidate_teachers(
        sid, gu_codes, spec, statuses, _RAW_POOL_LIMIT
    )
    raw_pool_size = len(cands)
    if not cands:
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=0, added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message="no_candidates_in_pool",
        )
        return {"sid": sid, "status": "skipped", "reason": "no_candidates_in_pool"}

    # b) cooldown — 오늘 추천 알림 ≥ cap 받은 선생님 사전 제외
    teacher_sids_in_pool = [str(c["teacher_sid"]) for c in cands]
    today_counts = await replica.count_today_teacher_recommendations(teacher_sids_in_pool)
    cap = settings.auto_dispatch_teacher_daily_cap
    filtered = [
        c for c in cands if today_counts.get(str(c["teacher_sid"]), 0) < cap
    ]
    cooldown_removed = raw_pool_size - len(filtered)
    if not filtered:
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=raw_pool_size, added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message=f"all_in_cooldown removed={cooldown_removed}",
        )
        return {"sid": sid, "status": "skipped", "reason": "all_in_cooldown",
                "pool_size": raw_pool_size, "cooldown_removed": cooldown_removed}

    # c) LLM 랭킹
    sched = _parse_schedule(app.get("schedule"))
    want_days = sched.get("days", [])
    cand_views = [_candidate_view(c, want_days) for c in filtered]

    # 일일 LLM 호출 한도 가드 (인사이트와 공유)
    ok, current = await get_llm_insight_store().check_and_increment_daily(
        limit=settings.llm_daily_limit
    )
    if not ok:
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=len(filtered), added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message=f"llm_daily_limit_exceeded current={current}",
        )
        return {"sid": sid, "status": "skipped", "reason": "llm_daily_limit_exceeded",
                "current": current}

    payload = _build_input(app, cand_views)
    try:
        raw_text, parsed, in_tok, out_tok = await llm.generate_recommendation(
            payload,
            max_tokens=_LLM_MAX_TOKENS,
            system_prompt=AUTO_DISPATCH_SYSTEM_PROMPT,
        )
        # JSON parse 실패 시 raw_text 앞 일부를 로그에 남겨 디버깅
        if not parsed and raw_text:
            logger.warning(
                "auto_dispatch LLM parse empty sid=%s len=%d head=%r tail=%r",
                sid, len(raw_text), raw_text[:200], raw_text[-200:],
            )
    except Exception as e:
        logger.exception("auto_dispatch LLM call failed sid=%s", sid)
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=len(filtered), added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message=f"llm_failed: {e!s}"[:1000],
        )
        return {"sid": sid, "status": "error", "error": "llm_failed"}

    try:
        await get_llm_insight_store().add_token_usage(in_tok, out_tok)
    except Exception:
        logger.exception("auto_dispatch token_usage persist failed sid=%s (graceful)", sid)

    ranked = parsed.get("ranked") or []
    # LLM이 추천 풀에 없는 sid를 끼워넣을 위험 방지
    valid_sids = {c["teacher_sid"] for c in cand_views}
    top_n = settings.auto_dispatch_top_n
    top_sids: list[str] = []
    top_names: list[str] = []
    for r in ranked:
        ts = str(r.get("teacher_sid") or "")
        if not ts or ts not in valid_sids or ts in top_sids:
            continue
        top_sids.append(ts)
        top_names.append(str(r.get("name") or ""))
        if len(top_sids) >= top_n:
            break

    if not top_sids:
        await store.record_run(
            recommendation_sid=sid, dry_run=dry_run,
            pool_size=len(filtered), added_count=0, succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message="llm_returned_empty_ranked",
        )
        return {"sid": sid, "status": "skipped", "reason": "llm_returned_empty_ranked",
                "pool_size": len(filtered)}

    # d) 콘솔 호출 (live) 또는 스킵 (dry-run)
    if dry_run:
        logger.info(
            "auto_dispatch DRY_RUN sid=%s pool=%d cooldown_removed=%d top=%d",
            sid, len(filtered), cooldown_removed, len(top_sids),
        )
        await store.record_run(
            recommendation_sid=sid, dry_run=True,
            pool_size=len(filtered), added_count=len(top_sids),
            succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
        )
        return {
            "sid": sid, "status": "dry_run",
            "pool_size": len(filtered),
            "cooldown_removed": cooldown_removed,
            "top": [{"teacher_sid": s, "name": n} for s, n in zip(top_sids, top_names)],
        }

    # live: add → memo → visit-offers (각 단계 graceful)
    err: str | None = None
    add_result: dict[str, Any] = {}
    denied: list[dict[str, Any]] = []
    try:
        add_result = await console.add_teachers(sid, top_sids)
    except ConsoleApiError as e:
        logger.error("auto_dispatch add_teachers failed sid=%s %s", sid, e)
        err = f"add_teachers_failed: status={e.status} body={e.body!r}"[:1000]
        await store.record_run(
            recommendation_sid=sid, dry_run=False,
            pool_size=len(filtered), added_count=len(top_sids),
            succeed_count=0, denied_count=0,
            llm_model_id=settings.llm_recommend_model_id,
            operator_email=operator_email,
            error_message=err,
        )
        return {"sid": sid, "status": "error", "error": err[:300]}

    # 자란다 콘솔 응답이 snake_case (application.yaml: property-naming-strategy: SNAKE_CASE).
    # 안전하게 두 표기 모두 호환.
    succeed_count = int(
        add_result.get("succeed_count") or add_result.get("succeedCount") or 0
    )

    # 메모 (graceful — 실패해도 visit-offers 진행)
    memo_ok = False
    if succeed_count > 0:
        memo_content = (
            f"[AI매칭 자동] LLM 추천 {succeed_count}명 추가 후 방문제안 발송"
        )
        try:
            await console.write_recommendation_memo([sid], memo_content)
            memo_ok = True
        except ConsoleApiError as e:
            logger.warning("auto_dispatch write_memo failed sid=%s %s", sid, e)
            err = f"memo_failed: status={e.status}"

    # visit-offers — 실제 알림 발송
    visit_offers_called = False
    if succeed_count > 0:
        try:
            denied = await console.send_visit_offers(sid, top_sids)
            visit_offers_called = True
        except ConsoleApiError as e:
            logger.error("auto_dispatch visit_offers failed sid=%s %s", sid, e)
            err = (err + " | " if err else "") + (
                f"visit_offers_failed: status={e.status} body={e.body!r}"[:600]
            )

    await store.record_run(
        recommendation_sid=sid, dry_run=False,
        pool_size=len(filtered), added_count=len(top_sids),
        succeed_count=succeed_count, denied_count=len(denied),
        llm_model_id=settings.llm_recommend_model_id,
        operator_email=operator_email,
        error_message=err,
    )

    return {
        "sid": sid,
        "status": "live" if visit_offers_called and not err else "partial",
        "pool_size": len(filtered),
        "cooldown_removed": cooldown_removed,
        "requested": len(top_sids),
        "succeed_count": succeed_count,
        "denied_count": len(denied),
        "memo_ok": memo_ok,
        "visit_offers_called": visit_offers_called,
        "error": err,
    }


def _make_summary(
    *,
    dry_run: bool,
    requested_cap: int,
    effective_targets: int,
    eligible: int,
    raw: int,
    excluded: int,
    processed: list[dict[str, Any]],
    skipped_reason: str | None,
    started_at: datetime,
    operator_email: str,
) -> dict[str, Any]:
    finished_at = datetime.now(KST)
    by_status: dict[str, int] = {}
    for p in processed:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    total_succeed = sum(int(p.get("succeed_count") or 0) for p in processed)
    total_denied = sum(int(p.get("denied_count") or 0) for p in processed)
    return {
        "dry_run": dry_run,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_sec": round((finished_at - started_at).total_seconds(), 2),
        "operator_email": operator_email,
        "requested_cap": requested_cap,
        "raw_candidates": raw,
        "excluded": excluded,
        "eligible": eligible,
        "effective_targets": effective_targets,
        "skipped_reason": skipped_reason,
        "by_status": by_status,
        "total_succeed_teachers": total_succeed,
        "total_denied_teachers": total_denied,
        "details": processed,
    }


async def _post_slack_summary(summary: dict[str, Any]) -> None:
    url = settings.auto_dispatch_slack_webhook.strip()
    if not url:
        return
    mode = "DRY_RUN" if summary["dry_run"] else "LIVE"
    lines = [
        f"*[AI매칭 자동] {mode}* — {summary['operator_email']}",
        f"raw={summary['raw_candidates']} excluded={summary['excluded']} "
        f"eligible={summary['eligible']} targets={summary['effective_targets']}",
        f"by_status={summary['by_status']} "
        f"succeed_teachers={summary['total_succeed_teachers']} "
        f"denied_teachers={summary['total_denied_teachers']} "
        f"elapsed={summary['elapsed_sec']}s",
    ]
    if summary.get("skipped_reason"):
        lines.append(f"skipped: {summary['skipped_reason']}")
    text = "\n".join(lines)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"text": text})
    except Exception:
        logger.exception("auto_dispatch slack post failed (graceful)")
