import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, KitchenAdoptedPlan, KitchenAlert
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import kitchen
from src.utils.security import hash_password
from src.utils.serialization import checksum


def seed_kitchen(db, dataset, admin):
    """演示档口登记、连续少备预警、合并沿革与已确认后的更正说明，可重复执行。"""
    if db.scalar(select(func.count()).select_from(KitchenAdoptedPlan)):
        return
    params = {'alpha': 0.35, 'horizon': 21, 'safety_pct': 12}
    result = analyze(dataset.records, params)
    job = db.scalar(select(Job).where(Job.name == '厨房备餐复核 · 21天'))
    if not job:
        job = Job(name='厨房备餐复核 · 21天', dataset_id=dataset.id, params=params, status='completed',
                  result=result, created_by=admin.id, created_at=time.time() - 1800,
                  finished_at=time.time() - 1790, approved=True)
        db.add(job)
        db.commit()
    plan = {row['date']: row['plan'] for row in result['table']['rows']}
    stall = '东港园午餐'
    days = [day for day in sorted(plan) if day <= '2026-11-27']

    class Seeder:
        email = 'seed@demo.local'
        role = 'admin'

    def submit(kind, payload, key):
        kitchen.submit(db, kind, stall, payload, 'seed-kq-' + key, Seeder)

    # 登记除 11-20 外各日实际采用的已批准建议（11-20 留作“缺采用建议”）。
    submit('adoption.upsert', {'job_id': job.id, 'days': [
        {'day': day, 'qty': plan[day]} for day in days if day != '2026-11-20'
    ]}, 'adopt-1')

    over = {'2026-11-09': 12, '2026-11-10': 9, '2026-11-11': 15,
            '2026-11-16': 10, '2026-11-17': 14, '2026-11-18': 11,
            '2026-11-21': 18, '2026-11-22': 16, '2026-11-23': 13,
            '2026-11-25': 12, '2026-11-26': 9, '2026-11-27': 15}
    under = {'2026-11-12': -8, '2026-11-13': -12, '2026-11-14': -20, '2026-11-15': -10}
    batches = []
    for day in sorted(plan):
        if day > '2026-11-27' or day == '2026-11-19':
            continue  # 11-19 故意不传实领，留作“缺实领批次”。
        if day == '2026-11-24':
            continue  # 先留缺口，稍后补传以演示两段合并。
        delta = over.get(day, under.get(day, 0))
        batches.append({'day': day, 'batch_ref': 'PO-' + day.replace('-', '') + '-1',
                        'qty': max(0, plan[day] + delta)})
    submit('batch.upsert', {'batches': batches, 'source': 'kitchen_requisition_202611.csv'}, 'batch-1')

    # 已确认预警：先确认，再更正 11-10 实领，保留原依据并追加更正说明。
    alert_first = db.scalar(select(KitchenAlert).where(
        KitchenAlert.stall == stall, KitchenAlert.start_day == '2026-11-09'))
    submit('alert.confirm', {'alert_id': alert_first.id,
                             'basis_checksum': checksum(alert_first.basis),
                             'note': '已与档口核对，当日加餐为研发活动加班餐。'}, 'confirm-1')
    submit('batch.upsert', {'batches': [{'day': '2026-11-10',
                                         'batch_ref': 'PO-20261110-1',
                                         'qty': plan['2026-11-10'] + 24,
                                         'note': '档口补录第二车实领'}],
                            'source': 'kitchen_requisition_202611_correction.csv'}, 'batch-fix-1')

    # 补传 11-24 实领，使 11-21~23 与 11-25~27 两个连续超量段合并为一条预警。
    submit('batch.upsert', {'batches': [{'day': '2026-11-24', 'batch_ref': 'PO-20261124-1',
                                         'qty': plan['2026-11-24'] + 10}],
                            'source': 'kitchen_requisition_202611_late.csv'}, 'batch-2')
    db.add(Audit(actor='seed', action='demo.kitchen', detail={'stall': stall}, at=time.time()))
    db.commit()


def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        admin=db.scalar(select(User).where(User.role=='admin'))
        seed_kitchen(db,d,admin)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,KitchenAdoptedPlan,KitchenAlert]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
