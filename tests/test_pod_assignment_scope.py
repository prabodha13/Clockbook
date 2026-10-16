import pytest
from fastapi import HTTPException
import database
import main
import models
import schemas


def tenant(session, tid):
    session.info['skip_tenant_scope'] = True
    obj = models.Tenant(id=tid, name=tid, slug=tid)
    session.add(obj)
    session.commit()
    session.info.pop('skip_tenant_scope', None)
    session.info['tenant_id'] = tid
    return obj


def member(session, tid, name, role, pod=None):
    session.info['skip_tenant_scope'] = True
    user = models.User(email=name, password_hash='test', default_tenant_id=tid)
    session.add(user)
    session.flush()
    obj = models.Member(tenant_id=tid, user_id=user.id, email=name, name=name, role=role, pod_id=pod, additional_permissions=[main.PERMISSION_MANAGE_PODS] if role == 'admin' else [])
    session.add(obj)
    session.commit()
    session.info.pop('skip_tenant_scope', None)
    session.info['tenant_id'] = tid
    return obj


def update(session, actor, target, pod):
    return main.update_member_pod(target.id, schemas.MemberPodUpdate(pod_id=pod, expected_version=target.version), current_member=actor, db=session)


def test_pod_assignment_respects_pod_and_tenant():
    s = database.SessionLocal()
    try:
        tid1 = 'pod_scope_tenant_one'
        tid2 = 'pod_scope_tenant_two'
        tenant(s, tid1)
        a = models.Pod(name='Pod A'); b = models.Pod(name='Pod B')
        s.add_all([a,b]); s.commit()
        admin = member(s, tid1, 'pod-admin@test.local', 'admin', a.id)
        own = member(s, tid1, 'pod-own@test.local', 'member', a.id)
        other = member(s, tid1, 'pod-other@test.local', 'member', b.id)
        no_pod_admin = member(s, tid1, 'pod-unassigned@test.local', 'admin')
        super_admin = member(s, tid1, 'pod-super@test.local', 'super_admin')
        update(s, admin, own, a.id)
        with pytest.raises(HTTPException) as ex:
            update(s, admin, other, a.id)
        assert ex.value.status_code == 403
        with pytest.raises(HTTPException) as ex:
            update(s, admin, own, b.id)
        assert ex.value.status_code == 403
        update(s, no_pod_admin, other, a.id)
        update(s, super_admin, other, b.id)
        tenant(s, tid2)
        external_pod = models.Pod(name='External Pod'); s.add(external_pod); s.commit()
        external = member(s, tid2, 'pod-external@test.local', 'member', external_pod.id)
        s.info['tenant_id'] = tid1
        with pytest.raises(HTTPException) as ex:
            update(s, super_admin, external, a.id)
        assert ex.value.status_code == 404
        with pytest.raises(HTTPException) as ex:
            update(s, super_admin, own, external_pod.id)
        assert ex.value.status_code == 404
    finally:
        s.close()
