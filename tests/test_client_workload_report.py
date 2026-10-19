"""Client workload aggregation and tenant/pod permission regression tests."""
from datetime import datetime
import pytest
from fastapi import HTTPException
import database
import main
import models
import schemas


def _tenant(s, tid):
    s.info['skip_tenant_scope'] = True
    s.add(models.Tenant(id=tid, name=tid, slug=tid))
    s.commit()
    s.info.pop('skip_tenant_scope', None)
    s.info['tenant_id'] = tid


def test_workload_tenant_client_pod_and_bank_average():
    s = database.SessionLocal()
    try:
        _tenant(s, 'client_workload_a')
        pa = models.Pod(name='Pod A'); pb = models.Pod(name='Pod B')
        s.add_all([pa,pb]);s.flush()
        a = models.Client(name='Alpha',code='A',pod_id=pa.id)
        b = models.Client(name='Beta',code='B',pod_id=pb.id)
        orphan = models.Client(name='Unassigned',code='U')
        s.add_all([a,b,orphan]);s.flush()
        owner = models.Member(name='Super',role='super_admin',email='super_w@example.com')
        admin = models.Member(name='Admin',role='admin',email='admin_w@example.com',pod_id=pa.id,additional_permissions=[main.PERMISSION_REPORT_CLIENT_WORKLOAD])
        other = models.Member(name='Other',role='admin',email='other_w@example.com',pod_id=pb.id,additional_permissions=[])
        s.add_all([owner,admin,other]);s.flush()
        def task(c,name,metric,bank,start,end,minutes,work):
            s.add(models.TaskInstance(client_id=c.id,client_name=c.name,name=name,task_type='Data Entry',status='submitted',submitted_at=datetime(2026,10,12),tracks_number_label=metric,bank_account_name=bank,start_count=start,end_count=end,adjusted_seconds=minutes*60,segments=[{'start':work+'T10:00:00Z','end':work+'T10:02:00Z'}]))
        task(a,'Reconcile bank','Unreconciled transactions','Stripe',200,100,5,'2026-10-01')
        task(a,'Reconcile bank','Unreconciled transactions','AIB',10,5,20,'2026-10-02')
        task(a,'Push Dext','Dext bills','',50,30,18,'2026-10-03')
        task(a,'Aged payables review','','',None,None,15,'2026-10-03')
        task(a,'Aged receivables review','','',None,None,12,'2026-10-04')
        task(b,'Other bank','Unreconciled transactions','Stripe',200,100,5,'2026-10-03')
        task(orphan,'Orphan','Dext bills','',40,10,60,'2026-10-03')
        s.commit()
        data = main.client_workload_report(grouping='monthly',current_member=admin,db=s)
        assert len(data['clients']) == 1
        x = data['clients'][0]
        assert x['name'] == 'Alpha'
        assert x['summary']['bank_count'] == 105
        assert x['summary']['dext_count'] == 20
        assert x['summary']['ap_seconds'] == 900
        assert x['summary']['ar_seconds'] == 720
        assert x['summary']['total_seconds'] == (5+20+18+15+12)*60
        assert len(x['periods']) == 1 and x['periods'][0]['period'] == '2026-10'
        accounts = {v['name']:v for v in x['summary']['bank_accounts']}
        assert accounts['Stripe']['minutes_per_item'] == 0.05
        assert accounts['AIB']['minutes_per_item'] == 4
        assert x['summary']['bank_minutes_per_item'] == round(25/105,3)
        assert main.client_workload_report(grouping='monthly',client_id=b.id,current_member=admin,db=s)['clients'] == []
        with pytest.raises(HTTPException) as exc:
            main.client_workload_report(current_member=other,db=s)
        assert exc.value.status_code == 403
        assert len(main.client_workload_report(current_member=owner,db=s)['clients']) == 3
        with pytest.raises(HTTPException) as exc:
            main.update_client_pod(a.id,schemas.ClientPodUpdate(pod_id=pb.id,expected_version=a.version),current_member=admin,db=s)
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            main.update_client_pod(a.id,schemas.ClientPodUpdate(pod_id='missing',expected_version=a.version),current_member=owner,db=s)
        assert exc.value.status_code == 404
        main.update_client_pod(a.id,schemas.ClientPodUpdate(pod_id=pb.id,expected_version=a.version),current_member=owner,db=s)
        assert not main.client_workload_report(current_member=admin,db=s)['clients']
        # Explicit tenant isolation despite an identical report permission.
        _tenant(s,'client_workload_b')
        other_tenant_super = models.Member(name='Other Super',role='super_admin',email='other_super_w@example.com')
        s.add(other_tenant_super); s.commit()
        assert main.client_workload_report(current_member=other_tenant_super,db=s)['clients'] == []
    finally:
        s.close()
