"""按档口登记采用建议、实领批次对账、连续少备预警与沿革追溯。

关键业务约束：
- 实领与“实际采用的已批准建议”逐日对账；缺采用或缺批次的日子标为待补齐，不参与连续超量判断。
- 连续 3 个完整日实领 > 采用建议才开预警；同一连续段只保留一条活动预警。
- 更正/补传后重算受影响区间；合并、拆分通过前驱/后继关系留痕。
- 未处理预警可撤销；已确认预警保留确认时依据，更正只追加说明并记录最新依据。
- 每个写操作有 operation_id 幂等键，同一键重试不会重复开预警或追加说明。
"""
from __future__ import annotations

import time
import uuid
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models import (
    Job,
    KitchenAdoptedPlan,
    KitchenAlert,
    KitchenAlertEvent,
    KitchenOperation,
    KitchenRequisitionBatch,
    KitchenStall,
)
from src.services.audit import record as audit_record
from src.utils.serialization import checksum

RULE_DAYS = 3
OPEN, CONFIRMED, REVOKED, SUPERSEDED = 'open', 'confirmed', 'revoked', 'superseded'
ACTIVE_ALERT_STATUS = (OPEN, CONFIRMED)
HANDLERS: dict = {}
MAX_SPAN_DAYS = 800


# ------------------------------------------------------------------ 基础工具

def _serialize_day(value) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise HTTPException(422, '日期须为 YYYY-MM-DD 格式')
    if not date(2000, 1, 1) <= parsed <= date(2100, 12, 31):
        raise HTTPException(422, '日期超出可追踪范围')
    return parsed.isoformat()


def _day_list(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    span = (last - first).days
    if span > MAX_SPAN_DAYS:
        raise HTTPException(422, f'单个档口的追踪区间不能超过 {MAX_SPAN_DAYS} 天')
    return [(first + timedelta(days=offset)).isoformat() for offset in range(span + 1)]


def _stall_name(value) -> str:
    name = str(value or '').strip()
    if not name:
        raise HTTPException(422, '档口不能为空')
    if len(name) > 80:
        raise HTTPException(422, '档口名称不能超过80个字符')
    return name


def _int_qty(value, label='数量') -> int:
    if isinstance(value, bool):
        raise HTTPException(422, f'{label}必须为非负整数')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f'{label}必须为非负整数')
    if not number.is_integer() or number < 0 or number > 1_000_000:
        raise HTTPException(422, f'{label}必须为非负整数')
    return int(number)


def _ensure_stall(db, stall) -> KitchenStall:
    name = _stall_name(stall)
    row = db.scalar(select(KitchenStall).where(KitchenStall.stall == name))
    if row:
        return row
    now = time.time()
    try:
        row = KitchenStall(stall=name, revision=1, created_at=now, updated_at=now)
        db.add(row)
        db.commit()
        return row
    except IntegrityError:
        db.rollback()
        return db.scalar(select(KitchenStall).where(KitchenStall.stall == name))


def _lock_stall(db, stall) -> KitchenStall:
    name = _stall_name(stall)
    row = db.scalar(select(KitchenStall).where(KitchenStall.stall == name).with_for_update())
    if row is None:
        raise HTTPException(409, '档口尚未建立对账记录，请先登记采用建议或实领批次')
    row.revision += 1
    row.updated_at = time.time()
    return row


def _ranges_intersect(start_a, end_a, start_b, end_b) -> bool:
    return start_a <= end_b and start_b <= end_a


def _event(db, alert, type_, actor, note='', detail=None, operation_id=None):
    db.add(KitchenAlertEvent(
        alert_id=alert.id, type=type_, version=alert.version, actor=actor,
        note=note, detail=detail or {}, at=time.time(), operation_id=operation_id,
    ))


# ------------------------------------------------------------------ 对账账本

def build_ledger(db, stall, start=None, end=None) -> list[dict]:
    name = _stall_name(stall)
    adoptions = {
        row.day: row for row in db.scalars(select(KitchenAdoptedPlan).where(
            KitchenAdoptedPlan.stall == name, KitchenAdoptedPlan.active.is_(True)))
    }
    batches: dict[str, list] = {}
    for row in db.scalars(select(KitchenRequisitionBatch).where(
            KitchenRequisitionBatch.stall == name, KitchenRequisitionBatch.active.is_(True))):
        batches.setdefault(row.day, []).append(row)
    if not adoptions and not batches:
        return []
    known = set(adoptions) | set(batches)
    low, high = min(known), max(known)
    if start:
        low = max(low, _serialize_day(start))
    if end:
        high = min(high, _serialize_day(end))
    if low > high:
        return []
    confirmed_ranges = [
        (row.start_day, row.end_day, row.alert_no, row.id) for row in db.scalars(select(KitchenAlert).where(
            KitchenAlert.stall == name, KitchenAlert.status == CONFIRMED))
    ]
    active_ranges = [
        (row.start_day, row.end_day, row.alert_no, row.id, row.status)
        for row in db.scalars(select(KitchenAlert).where(
            KitchenAlert.stall == name, KitchenAlert.status.in_(ACTIVE_ALERT_STATUS)))
    ]
    rows = []
    for day in _day_list(low, high):
        adoption = adoptions.get(day)
        day_batches = sorted(batches.get(day, []), key=lambda item: item.id)
        batch_total = sum(item.qty for item in day_batches)
        if adoption is None and not day_batches:
            status = 'missing'
        elif adoption is None:
            status = 'pending_plan'
        elif not day_batches:
            status = 'pending_batch'
        else:
            status = 'reconciled'
        recommended = adoption.recommended if adoption else None
        adopted = adoption.qty if adoption else None
        deviation = batch_total - recommended if recommended is not None and status == 'reconciled' else None
        adopted_deviation = batch_total - adopted if adopted is not None and status == 'reconciled' else None
        # 预警基线是“实际采用的已批准建议”：采用份数调整后以调整值为准。
        over = bool(status == 'reconciled' and adopted is not None and batch_total > adopted)
        confirmed_alerts = [item for item in confirmed_ranges if item[0] <= day <= item[1]]
        active_alerts = [item for item in active_ranges if item[0] <= day <= item[1]]
        rows.append({
            'day': day,
            'status': status,
            'recommended': recommended,
            'adopted': adopted,
            'actual': batch_total if day_batches else None,
            'deviation': deviation,
            'adopted_deviation': adopted_deviation,
            'over': over,
            'adjusted': bool(adoption and adoption.adjusted),
            'reason': adoption.reason if adoption else '',
            'adoption_revision': adoption.revision if adoption else None,
            'adoption_id': adoption.id if adoption else None,
            'job_id': adoption.job_id if adoption else None,
            'batch_refs': [item.batch_ref for item in day_batches],
            'batch_ids': [item.id for item in day_batches],
            'batching': [{'batch_ref': item.batch_ref, 'qty': item.qty, 'revision': item.revision,
                          'source': item.source, 'note': item.note} for item in day_batches],
            'confirmed_alert_ids': [item[3] for item in confirmed_alerts],
            'active_alert_ids': [item[3] for item in active_alerts],
            'active_alert_nos': [item[2] for item in active_alerts],
        })
    return rows


def _basis(rows, start, end) -> dict:
    days = [row for row in rows if start <= row['day'] <= end]
    recommended = sum(row['recommended'] or 0 for row in days)
    adopted = sum(row['adopted'] or 0 for row in days)
    actual = sum(row['actual'] or 0 for row in days)
    return {
        'rule': 'actual_gt_adopted',
        'rule_days': RULE_DAYS,
        'start_day': start,
        'end_day': end,
        'days': [{
            'day': row['day'], 'status': row['status'], 'recommended': row['recommended'],
            'adopted': row['adopted'], 'actual': row['actual'], 'deviation': row['deviation'],
            'adopted_deviation': row['adopted_deviation'], 'batch_refs': row['batch_refs'],
        } for row in days],
        'totals': {'recommended': recommended, 'adopted': adopted, 'actual': actual,
                   'over': actual - adopted},
    }


def ledger_summary(rows) -> dict:
    return {
        'tracked_days': len(rows),
        'reconciled_days': sum(row['status'] == 'reconciled' for row in rows),
        'pending_plan_days': sum(row['status'] == 'pending_plan' for row in rows),
        'pending_batch_days': sum(row['status'] == 'pending_batch' for row in rows),
        'missing_days': sum(row['status'] == 'missing' for row in rows),
        'over_days': sum(row['over'] for row in rows),
        'from_day': rows[0]['day'] if rows else None,
        'to_day': rows[-1]['day'] if rows else None,
    }


def _over_runs(rows, covered_days) -> list[dict]:
    runs, current = [], []
    for row in rows:
        if row['over'] and row['day'] not in covered_days:
            current.append(row['day'])
        else:
            if current:
                runs.append({'start': current[0], 'end': current[-1], 'days': current})
            current = []
    if current:
        runs.append({'start': current[0], 'end': current[-1], 'days': current})
    return [run for run in runs if len(run['days']) >= RULE_DAYS]


def _settled_days(db, stall, rows) -> set[str]:
    """已确认或已撤销的日期不再重复触发预警，避免确认/撤销被后续补传悄悄推翻。

    人工撤销只对“依据未再变化”的日期生效：若该区间后来被更正，应允许按新依据重新评估。
    """
    covered = set()
    low, high = (rows[0]['day'], rows[-1]['day']) if rows else (None, None)
    if not low:
        return covered
    for alert in db.scalars(select(KitchenAlert).where(
            KitchenAlert.stall == stall, KitchenAlert.status.in_((CONFIRMED, REVOKED)))):
        if alert.status == REVOKED and alert.auto_revoked:
            continue  # 系统撤销只是当时账本的结果，后续补传应允许重新评估。
        if _ranges_intersect(alert.start_day, alert.end_day, low, high):
            if alert.status == REVOKED:
                current = _basis(rows, alert.start_day, alert.end_day)
                if checksum(current) != checksum(alert.basis):
                    continue
            covered.update(day for day in _day_list(alert.start_day, alert.end_day)
                           if low <= day <= high)
    return covered


def recompute(db, stall, actor, operation_id=None, changed_days=None) -> dict:
    """按当前有效版本重算整个档口，并把预警差异落成带前驱/后继关系的沿革。"""
    name = _stall_name(stall)
    rows = build_ledger(db, name)
    result = {
        'stall': name,
        'changed_days': sorted(changed_days or []),
        'summary': ledger_summary(rows),
        'alerts': {'opened': [], 'updated': [], 'superseded': [], 'revoked': [], 'correction_notes': []},
    }
    if not rows:
        result['affected'] = {'from': None, 'to': None}
        return result

    low, high = rows[0]['day'], rows[-1]['day']
    result['affected'] = {
        'from': min([low] + list(changed_days or [])),
        'to': max([high] + list(changed_days or [])),
    }
    alerts = list(db.scalars(select(KitchenAlert).where(
        KitchenAlert.stall == name, KitchenAlert.status.in_(ACTIVE_ALERT_STATUS))))
    confirmed_alerts = [alert for alert in alerts if alert.status == CONFIRMED]
    open_alerts = [alert for alert in alerts if alert.status == OPEN]
    now = time.time()

    # 已确认预警：保留原依据；依据变化只追加更正说明。
    for alert in confirmed_alerts:
        basis = _basis(rows, alert.start_day, alert.end_day)
        previous = alert.latest_basis if alert.latest_basis is not None else alert.basis
        if checksum(previous) == checksum(basis):
            continue
        alert.latest_basis = basis
        alert.correction_count += 1
        alert.updated_at = now
        _event(db, alert, 'correction_note', actor,
               note='更正后重算该已确认区间，原确认依据保留。',
               detail={'previous_basis': previous, 'latest_basis': basis,
                       'previous_totals': (previous or {}).get('totals'),
                       'latest_totals': basis['totals']},
               operation_id=operation_id)
        result['alerts']['correction_notes'].append(alert.id)

    covered = _settled_days(db, name, rows)
    runs = _over_runs(rows, covered)
    # 先匹配完全一致的活动预警（只更新依据），其余区间再建立合并/拆分接续关系。
    exact_pairs, fresh_runs = [], []
    claimed: set[int] = set()
    for run in runs:
        exact = next((alert for alert in open_alerts
                      if alert.id not in claimed
                      and alert.start_day == run['start'] and alert.end_day == run['end']), None)
        if exact:
            claimed.add(exact.id)
            exact_pairs.append((exact, run))
        else:
            fresh_runs.append(run)

    for alert, run in exact_pairs:
        basis = _basis(rows, run['start'], run['end'])
        if checksum(alert.basis) == checksum(basis):
            continue
        previous_basis = alert.basis
        alert.basis = basis
        alert.version += 1
        alert.updated_at = now
        _event(db, alert, 'recalculated', actor,
               note='补传或更正后重算连续超量区间。',
               detail={'previous_basis': previous_basis, 'latest_basis': basis,
                       'previous_version': alert.version - 1, 'version': alert.version},
               operation_id=operation_id)
        result['alerts']['updated'].append(alert.id)

    created: list[tuple[KitchenAlert, list[KitchenAlert]]] = []
    for run in fresh_runs:
        basis = _basis(rows, run['start'], run['end'])
        predecessors = [alert for alert in open_alerts
                        if _ranges_intersect(alert.start_day, alert.end_day, run['start'], run['end'])]
        alert = KitchenAlert(
            stall=name, start_day=run['start'], end_day=run['end'], status=OPEN,
            version=1, basis=basis, latest_basis=None,
            predecessor_ids=sorted(item.id for item in predecessors),
            successor_ids=[], correction_count=0, created_at=now, updated_at=now,
        )
        db.add(alert)
        db.flush()
        alert.alert_no = f'KQ-{alert.id:06d}'
        created.append((alert, predecessors))
        result['alerts']['opened'].append(alert.id)

    for alert, predecessors in created:
        if isinstance(alert, KitchenAlert):
            predecessors = [item for item in predecessors if isinstance(item, KitchenAlert)]
        if predecessors:
            covers_all = all(alert.start_day <= item.start_day and alert.end_day >= item.end_day
                             for item in predecessors)
            if len(predecessors) > 1:
                type_, note = 'merged', '多个连续超量段合并为新的预警，前驱关系已保留。'
            elif covers_all:
                type_, note = 'extended', '更正后连续超量区间变化，由新版本接续原预警。'
            else:
                type_, note = 'split', '更正后原连续超量段拆分，由新版本接续原预警。'
        else:
            type_, note = 'opened', '连续实数领超过建议，触发预警。'
        _event(db, alert, type_, actor, note=note,
               detail={'basis': alert.basis,
                       'predecessors': [item.alert_no for item in predecessors]},
               operation_id=operation_id)
        for item in predecessors:
            first_time = item.status == OPEN
            item.status = SUPERSEDED
            item.successor_ids = sorted(set(item.successor_ids or []) | {alert.id})
            item.updated_at = now
            if first_time:
                item.superseded_reason = f'区间由 {alert.alert_no} 接续（合并/拆分）'
                _event(db, item, 'superseded', actor,
                       note=f'由 {alert.alert_no} 合并或拆分接续，原依据保留。',
                       detail={'successor_ids': item.successor_ids,
                               'successor_alert_nos': [alert.alert_no]},
                       operation_id=operation_id)
                result['alerts']['superseded'].append(item.id)
            elif len(item.successor_ids) > 1:
                _event(db, item, 'successor_added', actor,
                       note=f'同一更正还拆分出 {alert.alert_no}，沿革关系同步补齐。',
                       detail={'successor_ids': item.successor_ids,
                               'successor_alert_no': alert.alert_no},
                       operation_id=operation_id)

    # 未处理的旧预警在更正后不再成立时自动撤销。
    for alert in open_alerts:
        if alert.id in claimed or alert.status != OPEN:
            continue
        alert.status = REVOKED
        alert.auto_revoked = True
        alert.revoked_reason = '补传或更正后该区间不再满足连续三天实领超建议'
        alert.updated_at = now
        _event(db, alert, 'revoked', actor, note=alert.revoked_reason,
               detail={'basis': alert.basis}, operation_id=operation_id)
        result['alerts']['revoked'].append(alert.id)

    db.flush()
    return result


# ------------------------------------------------------------------ 写操作登记

def _register_adoptions(db, payload, actor) -> dict:
    stall = _stall_name(payload.get('stall'))
    days = payload.get('days') or []
    if not days:
        raise HTTPException(422, '请至少登记一天的采用建议')
    job = db.get(Job, payload.get('job_id'))
    if not job:
        raise HTTPException(404, '分析任务不存在')
    if job.status != 'completed' or not job.approved:
        raise HTTPException(409, '只能登记已完成且已获批准的建议')
    plan = {row['date']: row for row in (job.result or {}).get('table', {}).get('rows', [])}
    if not plan:
        raise HTTPException(409, '该分析结果没有可采用的建议明细')
    from src.models import Dataset
    dataset = db.get(Dataset, job.dataset_id)
    meals = {str(row.get('meal', '')).strip() for row in (dataset.records if dataset else [])}
    if meals != {stall}:
        raise HTTPException(422, f'该建议来自档口「{"、".join(sorted(meals)) or "未知"}」，与登记档口不一致')

    changed = []
    seen = set()
    for index, item in enumerate(days):
        day = _serialize_day(item.get('day'))
        if day in seen:
            raise HTTPException(422, f'第 {index + 1} 行日期重复')
        seen.add(day)
        if day not in plan:
            raise HTTPException(422, f'{day} 不在该批准建议的预测区间内')
        recommended = int(plan[day]['plan'])
        qty = _int_qty(item.get('qty', recommended), '实际采用份数')
        reason = str(item.get('reason') or '').strip()
        note = str(item.get('note') or '').strip()
        if len(reason) > 500 or len(note) > 500:
            raise HTTPException(422, '调整原因和备注不能超过500个字符')
        if qty != recommended and len(reason) < 2:
            raise HTTPException(422, f'{day} 采用份数与建议不同，须填写调整原因')
        current = db.scalar(select(KitchenAdoptedPlan).where(
            KitchenAdoptedPlan.stall == stall, KitchenAdoptedPlan.day == day,
            KitchenAdoptedPlan.active.is_(True)).with_for_update())
        if current and current.qty == qty and current.recommended == recommended and current.job_id == job.id:
            continue
        revision = (current.revision + 1) if current else 1
        row = KitchenAdoptedPlan(
            stall=stall, day=day, qty=qty, recommended=recommended,
            forecast=float(plan[day].get('forecast')) if plan[day].get('forecast') is not None else None,
            job_id=job.id, revision=revision, active=True, adjusted=qty != recommended,
            reason=reason, note=note, created_by=actor, created_at=time.time(),
            operation_id=payload.get('operation_id'),
        )
        db.add(row)
        db.flush()
        if current:
            current.active = False
            current.superseded_by = row.id
        changed.append(day)
    return {'changed_days': changed, 'stall': stall, 'job_id': job.id}


def _register_batches(db, payload, actor) -> dict:
    stall = _stall_name(payload.get('stall'))
    batches = payload.get('batches') or []
    if not batches:
        raise HTTPException(422, '请至少登记一条实领批次')
    source = str(payload.get('source') or 'manual.json').strip()[:200]
    changed = []
    seen = set()
    for index, item in enumerate(batches):
        day = _serialize_day(item.get('day'))
        ref = str(item.get('batch_ref') or '').strip()
        if not ref:
            raise HTTPException(422, f'第 {index + 1} 行缺少批次号')
        if len(ref) > 80:
            raise HTTPException(422, '批次号不能超过80个字符')
        if (day, ref) in seen:
            raise HTTPException(422, f'第 {index + 1} 行日期与批次号重复')
        seen.add((day, ref))
        qty = _int_qty(item.get('qty'), '实领份数')
        note = str(item.get('note') or '').strip()
        if len(note) > 500:
            raise HTTPException(422, '备注不能超过500个字符')
        current = db.scalar(select(KitchenRequisitionBatch).where(
            KitchenRequisitionBatch.stall == stall, KitchenRequisitionBatch.batch_ref == ref,
            KitchenRequisitionBatch.active.is_(True)).with_for_update())
        if current and current.day == day and current.qty == qty:
            continue
        revision = (current.revision + 1) if current else 1
        row = KitchenRequisitionBatch(
            stall=stall, day=day, batch_ref=ref, qty=qty, revision=revision, active=True,
            source=source, note=note, created_by=actor, created_at=time.time(),
            operation_id=payload.get('operation_id'),
        )
        db.add(row)
        db.flush()
        if current:
            current.active = False
            current.superseded_by = row.id
        changed.append(day)
    return {'changed_days': changed, 'stall': stall, 'source': source}


def _confirm_alert(db, payload, actor) -> dict:
    alert = db.scalar(select(KitchenAlert).where(KitchenAlert.id == payload.get('alert_id')).with_for_update())
    if not alert:
        raise HTTPException(404, '预警不存在')
    if alert.status != OPEN:
        raise HTTPException(409, f'仅未处理预警可确认，当前状态为 {alert.status}')
    expected = str(payload.get('basis_checksum') or '')
    if not expected:
        raise HTTPException(422, '确认时必须提交所依据版本的依据校验值')
    if expected != checksum(alert.basis):
        raise HTTPException(409, '预警依据已被更正更新，确认旧依据被拒绝；请刷新后按最新依据确认')
    note = str(payload.get('note') or '').strip()
    if len(note) > 1000:
        raise HTTPException(422, '处理说明不能超过1000个字符')
    alert.status = CONFIRMED
    alert.confirmed_basis = alert.basis
    alert.latest_basis = alert.basis
    alert.confirmed_by = actor
    alert.confirmed_at = time.time()
    alert.confirmed_note = note
    alert.updated_at = time.time()
    _event(db, alert, 'confirmed', actor, note=note or '确认按该依据处理',
           detail={'basis': alert.basis, 'basis_checksum': expected},
           operation_id=payload.get('operation_id'))
    return {'alert_id': alert.id, 'status': alert.status, 'basis_checksum': expected}


def _revoke_alert(db, payload, actor) -> dict:
    alert = db.scalar(select(KitchenAlert).where(KitchenAlert.id == payload.get('alert_id')).with_for_update())
    if not alert:
        raise HTTPException(404, '预警不存在')
    if alert.status != OPEN:
        raise HTTPException(409, '仅未处理预警可撤销；已确认记录须保留原依据')
    reason = str(payload.get('reason') or '').strip()
    if len(reason) < 2:
        raise HTTPException(422, '撤销未处理预警须填写原因')
    if len(reason) > 1000:
        raise HTTPException(422, '撤销原因不能超过1000个字符')
    alert.status = REVOKED
    alert.revoked_reason = reason
    alert.updated_at = time.time()
    _event(db, alert, 'revoked', actor, note=reason,
           detail={'basis': alert.basis}, operation_id=payload.get('operation_id'))
    return {'alert_id': alert.id, 'status': alert.status}


def _adoption_handler(db, payload, actor):
    info = _register_adoptions(db, payload, actor)
    report = recompute(db, info['stall'], actor, payload.get('operation_id'), info['changed_days'])
    return {**info, **report}


def _batch_handler(db, payload, actor):
    info = _register_batches(db, payload, actor)
    report = recompute(db, info['stall'], actor, payload.get('operation_id'), info['changed_days'])
    return {**info, **report}


def _recompute_handler(db, payload, actor):
    return recompute(db, payload.get('stall'), actor, payload.get('operation_id'))


HANDLERS.update({
    'adoption.upsert': _adoption_handler,
    'batch.upsert': _batch_handler,
    'alert.confirm': _confirm_alert,
    'alert.revoke': _revoke_alert,
    'ledger.recompute': _recompute_handler,
})
WRITE_ROLES = {'adoption.upsert': ('admin', 'analyst'), 'batch.upsert': ('admin', 'analyst'),
               'alert.confirm': ('admin',), 'alert.revoke': ('admin',),
               'ledger.recompute': ('admin',)}


def _payload_kind_roles(kind, role):
    if kind not in HANDLERS:
        raise HTTPException(422, '不支持的操作类型')
    if role not in WRITE_ROLES[kind]:
        raise HTTPException(403, '当前角色无权执行该操作')


def _find_operation(db, operation_id):
    return db.scalar(select(KitchenOperation).where(KitchenOperation.operation_id == operation_id))


def submit(db, kind, stall, payload, operation_id, user, actor=None) -> tuple[dict, bool]:
    """幂等提交：同一 operation_id 与相同载荷只执行一次；载荷改变返回 409。"""
    _payload_kind_roles(kind, user.role)
    name = _stall_name(stall)
    key = str(operation_id or uuid.uuid4().hex)
    if len(key) > 80 or len(key) < 8:
        raise HTTPException(422, '幂等键长度须为8~80个字符')
    body = {**payload, 'stall': name, 'operation_id': key}
    body_hash = checksum(body)
    _ensure_stall(db, name)
    op = _find_operation(db, key)
    if op:
        if op.kind != kind or op.payload_hash != body_hash:
            raise HTTPException(409, '同一幂等键已用于不同的更正内容，请使用新的操作编号')
        if op.status == 'completed':
            return op.result, True
        if op.status == 'failed' and op.error_status:
            raise HTTPException(op.error_status, op.error)
    else:
        now = time.time()
        who = actor or user.email
        db.add(KitchenOperation(
            operation_id=key, kind=kind, stall=name, payload=body, payload_hash=body_hash,
            status='pending', attempts=0, result={}, error='', error_status=0,
            actor=who, actor_role=user.role, created_at=now, updated_at=now,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
    return _execute(db, key, raise_http=True)


def _execute(db, key, raise_http=True):
    """执行单条操作。业务校验失败无副作用时删除半成品记录，便于修正输入后换新键重试。"""
    op = db.scalar(select(KitchenOperation).where(KitchenOperation.operation_id == key).with_for_update())
    if op is None:
        # 并发提交同一幂等键时可能插入冲突后短暂读不到尚未提交的记录，按可重试冲突返回。
        raise HTTPException(409, '该操作正在并发处理中，请稍后用同一操作编号重试')
    if op.status == 'completed':
        db.commit()
        return op.result, True
    if op.status == 'failed' and op.error_status:
        detail, code = op.error, op.error_status
        db.commit()
        raise HTTPException(code, detail)
    op.attempts += 1
    op.updated_at = time.time()
    actor = op.actor
    try:
        _lock_stall(db, op.stall)
        result = HANDLERS[op.kind](db, op.payload, actor)
        op.status = 'completed'
        op.result = result
        op.error = ''
        op.error_status = 0
        op.completed_at = time.time()
        op.updated_at = op.completed_at
        audit_record(db, actor, 'kitchen.' + op.kind.replace('.', '_'),
                     {'operation_id': key, 'stall': op.stall,
                      'changed_days': result.get('changed_days', []),
                      'alerts': result.get('alerts', {})})
        db.commit()
        return result, False
    except HTTPException as exc:
        db.rollback()
        _discard_operation(db, key)  # 4xx 无副作用：释放幂等键，避免留下不可重放的半成品。
        if raise_http:
            raise
        return {'error': str(exc.detail), 'status': exc.status_code}, False
    except Exception as exc:
        # 非业务错误可能已产生部分写入但已回滚；保留 pending 供恢复任务重放。
        db.rollback()
        row = _find_operation(db, key)
        if row is not None:
            row.status = 'pending'
            row.error = f'内部错误：{str(exc)[:900]}'
            row.updated_at = time.time()
            db.commit()
        if raise_http:
            raise HTTPException(503, '操作执行中断，已登记恢复；稍后重试同一操作编号或到中断恢复重放')
        return {'error': '内部错误', 'status': 0}, False


def _discard_operation(db, key):
    op = _find_operation(db, key)
    if op is not None and op.status != 'completed':
        db.delete(op)
        db.commit()


def recover_operations(db=None, limit=20, stale_seconds=0) -> list[str]:
    """恢复中断的更正：重放 pending 或可重试失败的操作，幂等键保证不重复产生预警。"""
    owned = db is None
    if owned:
        from src.config.database import Session
        db = Session()
    try:
        cutoff = time.time() - stale_seconds
        rows = list(db.scalars(select(KitchenOperation).where(
            KitchenOperation.status.in_(('pending', 'failed')),
            KitchenOperation.updated_at <= cutoff,
        ).order_by(KitchenOperation.id).limit(limit)))
        keys = [row.operation_id for row in rows if row.status == 'pending' or not row.error_status]
        db.commit()
        recovered = []
        for key in keys:
            try:
                _execute(db, key, raise_http=False)
                recovered.append(key)
            except Exception:
                db.rollback()
        return recovered
    finally:
        if owned:
            db.close()


def replay_pending_for_worker() -> list[str]:
    recovered = []
    for key in recover_operations(limit=20, stale_seconds=30):
        recovered.append(key)
    return recovered
