from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from src.config.database import get_db
from src.middlewares.auth import admin_only, can_write, current_user
from src.models import (
    Job,
    KitchenAdoptedPlan,
    KitchenAlert,
    KitchenAlertEvent,
    KitchenOperation,
    KitchenRequisitionBatch,
)
from src.services import kitchen
from src.services.audit import record
from src.utils.serialization import checksum

router = APIRouter(prefix='/kitchen')


class WriteInput(BaseModel):
    operation_id: str = Field(min_length=8, max_length=80)
    stall: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)


def _job_target_options(db):
    options = []
    approved = db.scalars(select(Job).where(
        Job.status == 'completed', Job.approved.is_(True)).order_by(Job.id.desc())).all()
    for job in approved[:12]:
        days = [row['date'] for row in (job.result or {}).get('table', {}).get('rows', [])]
        options.append({'job_id': job.id, 'name': job.name, 'params': job.params,
                        'from_day': days[0] if days else '', 'to_day': days[-1] if days else '',
                        'days': len(days)})
    return options


def _alert_dict(db, alert, include_events=False):
    predecessors = list(db.scalars(select(KitchenAlert).where(
        KitchenAlert.id.in_(alert.predecessor_ids or [0]))))
    successors = list(db.scalars(select(KitchenAlert).where(
        KitchenAlert.id.in_(alert.successor_ids or [0]))))
    basis = alert.latest_basis if alert.latest_basis is not None else alert.basis
    data = {
        'id': alert.id, 'alert_no': alert.alert_no, 'stall': alert.stall,
        'start_day': alert.start_day, 'end_day': alert.end_day,
        'rule_days': (alert.basis or {}).get('rule_days', kitchen.RULE_DAYS),
        'status': alert.status, 'version': alert.version,
        'basis': alert.basis, 'latest_basis': alert.latest_basis,
        'basis_checksum': checksum(alert.basis),
        'latest_basis_checksum': checksum(basis),
        'totals': (alert.basis or {}).get('totals', {}),
        'latest_totals': (basis or {}).get('totals', {}),
        'confirmed_basis': alert.confirmed_basis, 'confirmed_by': alert.confirmed_by,
        'confirmed_at': alert.confirmed_at, 'confirmed_note': alert.confirmed_note,
        'revoked_reason': alert.revoked_reason, 'superseded_reason': alert.superseded_reason,
        'predecessors': [{'id': item.id, 'alert_no': item.alert_no, 'start_day': item.start_day,
                          'end_day': item.end_day, 'status': item.status} for item in predecessors],
        'successors': [{'id': item.id, 'alert_no': item.alert_no, 'start_day': item.start_day,
                        'end_day': item.end_day, 'status': item.status} for item in successors],
        'correction_count': alert.correction_count,
        'basis_changed': alert.status == kitchen.CONFIRMED and alert.latest_basis is not None
                         and checksum(alert.latest_basis) != checksum(alert.basis),
        'created_at': alert.created_at, 'updated_at': alert.updated_at,
    }
    if include_events:
        data['events'] = [{
            'id': e.id, 'type': e.type, 'version': e.version, 'actor': e.actor,
            'note': e.note, 'detail': e.detail, 'at': e.at, 'operation_id': e.operation_id,
        } for e in db.scalars(select(KitchenAlertEvent).where(
            KitchenAlertEvent.alert_id == alert.id).order_by(KitchenAlertEvent.id))]
    return data


def _stall_names(db):
    names = list(db.scalars(select(KitchenAdoptedPlan.stall).distinct()))
    names += list(db.scalars(select(KitchenRequisitionBatch.stall).distinct()))
    return sorted(set(names))


@router.get('/stalls')
def stalls(db=Depends(get_db), u=Depends(current_user)):
    result = []
    for name in _stall_names(db):
        rows = kitchen.build_ledger(db, name)
        open_count = db.scalar(select(func.count()).select_from(KitchenAlert).where(
            KitchenAlert.stall == name, KitchenAlert.status == kitchen.OPEN))
        pending = sum(row['status'] != 'reconciled' for row in rows)
        result.append({'stall': name, 'days': len(rows), 'pending_days': pending,
                       'over_days': sum(row['over'] for row in rows), 'open_alerts': open_count,
                       'from_day': rows[0]['day'] if rows else '',
                       'to_day': rows[-1]['day'] if rows else ''})
    return result


@router.get('/ledger')
def ledger(stall: str, start: str | None = None, end: str | None = None,
           db=Depends(get_db), u=Depends(current_user)):
    name = kitchen._stall_name(stall)
    rows = kitchen.build_ledger(db, name, start=start, end=end)
    return {
        'stall': name,
        'summary': kitchen.ledger_summary(rows),
        'rows': sorted(rows, key=lambda row: row['day'], reverse=True),
        'jobs': _job_target_options(db),
        'suggested_days': [row['day'] for row in rows if row['status'] == 'pending_plan'],
    }


@router.get('/pending')
def pending(stall: str | None = None, db=Depends(get_db), u=Depends(current_user)):
    """待补齐清单：缺采用建议、缺实领批次或整日缺失。"""
    names = [kitchen._stall_name(stall)] if stall else _stall_names(db)
    items = []
    for name in names:
        rows = kitchen.build_ledger(db, name)
        lacks = {'pending_plan': ['adoption'], 'pending_batch': ['batch'],
                 'missing': ['adoption', 'batch']}
        for row in rows:
            if row['status'] == 'reconciled':
                continue
            items.append({
                'stall': name, 'day': row['day'], 'status': row['status'],
                'needs': lacks[row['status']],
                'recommended': row['recommended'], 'adopted': row['adopted'], 'actual': row['actual'],
                'batch_refs': row['batch_refs'], 'adoption_id': row['adoption_id'],
            })
    items.sort(key=lambda item: (item['day'], item['stall']), reverse=True)
    return items


@router.get('/alerts')
def alerts(stall: str | None = None, status: str | None = None,
           db=Depends(get_db), u=Depends(current_user)):
    query = select(KitchenAlert).order_by(KitchenAlert.id.desc())
    if stall:
        query = query.where(KitchenAlert.stall == kitchen._stall_name(stall))
    if status:
        if status not in (kitchen.OPEN, kitchen.CONFIRMED, kitchen.REVOKED, kitchen.SUPERSEDED):
            raise HTTPException(422, '预警状态须为 open、confirmed、revoked 或 superseded')
        query = query.where(KitchenAlert.status == status)
    return [_alert_dict(db, alert) for alert in db.scalars(query.limit(200))]


@router.get('/alerts/{id}')
def alert_detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    alert = db.get(KitchenAlert, id)
    if not alert:
        raise HTTPException(404, '预警不存在')
    return _alert_dict(db, alert, include_events=True)


@router.post('/adoptions')
def adopt(data: WriteInput, db=Depends(get_db), u=Depends(can_write)):
    result, duplicate = kitchen.submit(db, 'adoption.upsert', data.stall, data.payload,
                                       data.operation_id, u)
    return {'duplicate': duplicate, 'result': result}


@router.post('/batches')
def batches(data: WriteInput, db=Depends(get_db), u=Depends(can_write)):
    result, duplicate = kitchen.submit(db, 'batch.upsert', data.stall, data.payload,
                                       data.operation_id, u)
    return {'duplicate': duplicate, 'result': result}


def _alert_or_404(db, alert_id):
    alert = db.get(KitchenAlert, alert_id)
    if not alert:
        raise HTTPException(404, '预警不存在')
    return alert


@router.post('/alerts/{id}/confirm')
def confirm(id: int, data: dict, db=Depends(get_db), u=Depends(admin_only)):
    alert = _alert_or_404(db, id)
    payload = {key: data[key] for key in ('basis_checksum', 'note') if key in data}
    result, duplicate = kitchen.submit(
        db, 'alert.confirm', alert.stall, {'alert_id': id, **payload},
        str(data.get('operation_id') or ''), u)
    return {'duplicate': duplicate, 'result': result}


@router.post('/alerts/{id}/revoke')
def revoke(id: int, data: dict, db=Depends(get_db), u=Depends(admin_only)):
    alert = _alert_or_404(db, id)
    payload = {key: data[key] for key in ('reason',) if key in data}
    result, duplicate = kitchen.submit(
        db, 'alert.revoke', alert.stall, {'alert_id': id, **payload},
        str(data.get('operation_id') or ''), u)
    return {'duplicate': duplicate, 'result': result}


@router.post('/recalculate')
def recalculate(data: dict, db=Depends(get_db), u=Depends(admin_only)):
    """手动重算档口账本；用于恢复后复核或补传批次的整体修复。"""
    stall = kitchen._stall_name(data.get('stall'))
    operation_id = str(data.get('operation_id') or '')
    if len(operation_id) < 8 or len(operation_id) > 80:
        raise HTTPException(422, '重算须提供8~80字符的幂等键')
    result, duplicate = kitchen.submit(db, 'ledger.recompute', stall, {}, operation_id, u)
    return {'duplicate': duplicate, 'result': result}


@router.get('/operations/{operation_id}')
def operation(operation_id: str, db=Depends(get_db), u=Depends(current_user)):
    op = db.scalar(select(KitchenOperation).where(KitchenOperation.operation_id == operation_id))
    if not op:
        raise HTTPException(404, '操作记录不存在')
    return {'operation_id': op.operation_id, 'kind': op.kind, 'stall': op.stall,
            'status': op.status, 'attempts': op.attempts, 'actor': op.actor,
            'result': op.result, 'error': op.error, 'created_at': op.created_at,
            'completed_at': op.completed_at}


@router.post('/operations/recover')
def recover(data: dict, db=Depends(get_db), u=Depends(admin_only)):
    """服务端恢复入口：按操作编号重放中断的更正，同一幂等键不会重复开预警或追加说明。"""
    operation_id = str(data.get('operation_id') or '')
    if len(operation_id) < 8 or len(operation_id) > 80:
        raise HTTPException(422, '恢复操作须提供8~80字符的操作编号')
    op = db.scalar(select(KitchenOperation).where(KitchenOperation.operation_id == operation_id))
    if not op:
        raise HTTPException(404, '操作记录不存在，请核对页面提示的操作编号')
    if op.kind not in kitchen.HANDLERS:
        raise HTTPException(409, '该操作类型不可重放')
    if op.status == 'completed':
        return {'replayed': [], 'duplicate': True, 'result': op.result}
    result, duplicate = kitchen._execute(db, operation_id, raise_http=True)
    record(db, u.email, 'kitchen.recover', {'operation_id': operation_id})
    db.commit()
    return {'replayed': [] if duplicate else [operation_id], 'duplicate': duplicate, 'result': result}


@router.get('/trail')
def trail(stall: str | None = None, day: str | None = None,
          db=Depends(get_db), u=Depends(current_user)):
    """追溯采用方案与实领批次的版本链，以及预警沿革和处理记录。"""
    if stall:
        stall = kitchen._stall_name(stall)
    if day:
        day = kitchen._serialize_day(day)
    plans_query = select(KitchenAdoptedPlan).order_by(KitchenAdoptedPlan.id.desc())
    batches_query = select(KitchenRequisitionBatch).order_by(KitchenRequisitionBatch.id.desc())
    alerts_query = select(KitchenAlert).order_by(KitchenAlert.id.desc())
    if stall:
        plans_query = plans_query.where(KitchenAdoptedPlan.stall == stall)
        batches_query = batches_query.where(KitchenRequisitionBatch.stall == stall)
        alerts_query = alerts_query.where(KitchenAlert.stall == stall)
    if day:
        plans_query = plans_query.where(KitchenAdoptedPlan.day == day)
        batches_query = batches_query.where(KitchenRequisitionBatch.day == day)
        alerts_query = alerts_query.where(KitchenAlert.start_day <= day, KitchenAlert.end_day >= day)

    timeline: dict[tuple[str, str], dict] = {}
    for kind, rows in (('adoption', db.scalars(plans_query.limit(120))),
                       ('batch', db.scalars(batches_query.limit(120)))):
        for row in rows:
            key = (row.stall, row.day)
            entry = timeline.setdefault(key, {'stall': row.stall, 'day': row.day, 'sources': []})
            source_key = 'adoption' if kind == 'adoption' else row.batch_ref
            source = next((item for item in entry['sources'] if item['key'] == source_key), None)
            if source is None:
                source = {'key': source_key, 'kind': kind, 'versions': []}
                entry['sources'].append(source)
            source['versions'].append({
                'id': row.id, 'qty': row.qty, 'revision': row.revision, 'active': row.active,
                'superseded_by': row.superseded_by,
                'recommended': getattr(row, 'recommended', None),
                'job_id': getattr(row, 'job_id', None),
                'source': getattr(row, 'source', ''),
                'reason': getattr(row, 'reason', ''), 'note': row.note,
                'created_by': row.created_by, 'created_at': row.created_at,
                'operation_id': row.operation_id,
            })

    alerts_out = []
    for alert in db.scalars(alerts_query.limit(120)):
        event_query = select(KitchenAlertEvent).where(
            KitchenAlertEvent.alert_id == alert.id).order_by(KitchenAlertEvent.id)
        events = [{'type': e.type, 'actor': e.actor, 'note': e.note, 'at': e.at,
                   'version': e.version, 'operation_id': e.operation_id}
                  for e in db.scalars(event_query)]
        alerts_out.append({**_alert_dict(db, alert), 'events': events})
    return {'stall': stall, 'day': day,
            'timelines': sorted(timeline.values(), key=lambda item: item['day'], reverse=True),
            'alerts': alerts_out}
