import pytest
from fastapi import HTTPException

import database
import main
import models
import schemas


def _tenant(session, tenant_id="tenant_admin_permissions"):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    tenant = session.get(models.Tenant, tenant_id)
    if tenant is None:
        tenant = models.Tenant(id=tenant_id, name="Admin permissions", slug=tenant_id)
        session.add(tenant)
        session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return tenant


def _member(session, tenant_id, email, role="member", pod_id=None):
    previous = session.info.get("skip_tenant_scope")
    session.info["skip_tenant_scope"] = True
    user = models.User(email=email, password_hash="x", default_tenant_id=tenant_id)
    session.add(user)
    session.flush()
    member = models.Member(
        tenant_id=tenant_id,
        user_id=user.id,
        name=email,
        email=email,
        role=role,
        pod_id=pod_id,
        additional_permissions=[],
    )
    session.add(member)
    session.commit()
    if previous is None:
        session.info.pop("skip_tenant_scope", None)
    else:
        session.info["skip_tenant_scope"] = previous
    session.info["tenant_id"] = tenant_id
    return member


def test_super_admin_can_grant_individual_report_permissions_and_staff_cannot_receive_them():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s)
        pod = models.Pod(name="Pod A")
        s.add(pod)
        s.commit()
        super_admin = _member(s, tenant.id, "perm-super@example.com", "super_admin")
        admin = _member(s, tenant.id, "perm-admin@example.com", "admin", pod.id)
        staff = _member(s, tenant.id, "perm-staff@example.com", "member", pod.id)

        with pytest.raises(HTTPException) as denied:
            main.help_events_summary(current_member=admin, db=s)
        assert denied.value.status_code == 403

        updated = main.update_member_additional_permissions(
            admin.id,
            schemas.MemberAdditionalPermissionsUpdate(
                permissions=[main.PERMISSION_REPORT_HELP, main.PERMISSION_INSIGHTS_LEAVE_CAPACITY],
                expected_version=admin.version,
            ),
            current_member=super_admin,
            db=s,
        )
        assert main.PERMISSION_REPORT_HELP in updated.additional_permissions
        assert main.PERMISSION_INSIGHTS_LEAVE_CAPACITY in updated.additional_permissions
        assert updated.can_view_leave_capacity_insights is True
        assert main.help_events_summary(current_member=updated, db=s) == []

        with pytest.raises(HTTPException) as bad_staff_grant:
            main.update_member_additional_permissions(
                staff.id,
                schemas.MemberAdditionalPermissionsUpdate(
                    permissions=[main.PERMISSION_REPORT_AUDIT],
                    expected_version=staff.version,
                ),
                current_member=super_admin,
                db=s,
            )
        assert bad_staff_grant.value.status_code == 400
    finally:
        s.close()


def test_admin_report_scope_stays_within_admin_pod():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_admin_permissions_scope")
        pod_a = models.Pod(name="Pod A")
        pod_b = models.Pod(name="Pod B")
        s.add_all([pod_a, pod_b])
        s.commit()
        super_admin = _member(s, tenant.id, "scope-super@example.com", "super_admin")
        admin = _member(s, tenant.id, "scope-admin@example.com", "admin", pod_a.id)
        a = _member(s, tenant.id, "scope-a@example.com", "member", pod_a.id)
        b = _member(s, tenant.id, "scope-b@example.com", "member", pod_b.id)

        admin = main.update_member_additional_permissions(
            admin.id,
            schemas.MemberAdditionalPermissionsUpdate(
                permissions=[main.PERMISSION_REPORT_HELP],
                expected_version=admin.version,
            ),
            current_member=super_admin,
            db=s,
        )
        s.add(models.HelpEvent(member_id=a.id, colleague_id=b.id, direction="helped", seconds=300, source="manual"))
        s.add(models.HelpEvent(member_id=b.id, colleague_id=a.id, direction="helped", seconds=600, source="manual"))
        s.commit()

        detail = main.help_events_detail(current_member=admin, db=s)
        assert len(detail) == 1
        assert detail[0].member_name == a.name
        assert detail[0].colleague_name == "Other team member"
    finally:
        s.close()


def test_downgrading_admin_removes_admin_only_permissions_but_keeps_insights_permission():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_admin_permissions_downgrade")
        super_admin = _member(s, tenant.id, "down-super@example.com", "super_admin")
        admin = _member(s, tenant.id, "down-admin@example.com", "admin")
        admin = main.update_member_additional_permissions(
            admin.id,
            schemas.MemberAdditionalPermissionsUpdate(
                permissions=[main.PERMISSION_REPORT_OVERRIDES, main.PERMISSION_INSIGHTS_LEAVE_CAPACITY],
                expected_version=admin.version,
            ),
            current_member=super_admin,
            db=s,
        )
        downgraded = main.update_member_role(
            admin.id,
            schemas.MemberRoleUpdate(role="member", expected_version=admin.version),
            current_member=super_admin,
            db=s,
        )
        assert downgraded.role == "member"
        assert downgraded.additional_permissions == [main.PERMISSION_INSIGHTS_LEAVE_CAPACITY]
        assert downgraded.can_view_leave_capacity_insights is True
    finally:
        s.close()


def test_admin_can_receive_selected_super_admin_capabilities_but_not_grant_permissions():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_admin_permissions_capabilities")
        super_admin = _member(s, tenant.id, "caps-super@example.com", "super_admin")
        admin = _member(s, tenant.id, "caps-admin@example.com", "admin")
        admin = main.update_member_additional_permissions(
            admin.id,
            schemas.MemberAdditionalPermissionsUpdate(
                permissions=[
                    main.PERMISSION_MANAGE_PODS,
                    main.PERMISSION_ADD_STAFF_MANUALLY,
                    main.PERMISSION_MANAGE_LEARNING_CATEGORIES,
                    main.PERMISSION_MANAGE_DELEGATION_EXCLUSIONS,
                    main.PERMISSION_MANAGE_WORKSPACE_BRANDING,
                    main.PERMISSION_MANAGE_INTEGRATIONS,
                    main.PERMISSION_MANAGE_AUDIT_RECORDING,
                ],
                expected_version=admin.version,
            ),
            current_member=super_admin,
            db=s,
        )
        pod = main.create_pod(schemas.PodCreate(name="Delegated pod"), current_member=admin, db=s)
        assert pod.name == "Delegated pod"
        category = main.create_learning_category(schemas.LearningCategoryCreate(name="Delegated learning"), current_member=admin, db=s)
        assert category.name == "Delegated learning"
        branding = main.update_workspace_branding(schemas.WorkspaceBrandingUpdate(logo_data_url=None), current_member=admin, db=s)
        assert branding["workspace_id"] == tenant.id
        with pytest.raises(HTTPException) as denied:
            main.update_member_additional_permissions(
                admin.id,
                schemas.MemberAdditionalPermissionsUpdate(permissions=[], expected_version=admin.version),
                current_member=admin,
                db=s,
            )
        assert denied.value.status_code == 403
    finally:
        s.close()



def test_manage_super_admins_adds_super_admins_to_read_scope_but_not_account_mutation_scope():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_manage_super_admin_visibility")
        super_admin = _member(s, tenant.id, "visible-super@example.com", "super_admin")
        granting_super_admin = _member(s, tenant.id, "granting-super@example.com", "super_admin")
        admin = _member(s, tenant.id, "visible-admin@example.com", "admin")
        staff = _member(s, tenant.id, "visible-staff@example.com", "member")

        before = main._insights_allowed_member_ids(admin, s)
        assert staff.id in before
        assert super_admin.id not in before

        admin = main.update_member_additional_permissions(
            admin.id,
            schemas.MemberAdditionalPermissionsUpdate(
                permissions=[main.PERMISSION_MANAGE_SUPER_ADMINS],
                expected_version=admin.version,
            ),
            current_member=granting_super_admin,
            db=s,
        )
        after = main._insights_allowed_member_ids(admin, s)
        assert staff.id in after
        assert super_admin.id in after
        assert granting_super_admin.id in after

        # Visibility never becomes authority to alter a Super Admin account.
        assert main._member_in_admin_scope(admin, super_admin) is False
        with pytest.raises(HTTPException) as denied:
            main.update_member_capacity(
                super_admin.id,
                schemas.MemberCapacityUpdate(weekly_capacity_hours=35, expected_version=super_admin.version),
                current_member=admin,
                db=s,
            )
        assert denied.value.status_code == 403
    finally:
        s.close()


def test_manage_super_admins_allows_admin_to_receive_super_admin_tasks_in_read_scope():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_manage_super_admin_tasks")
        granting_super_admin = _member(s, tenant.id, "task-granting-super@example.com", "super_admin")
        visible_super_admin = _member(s, tenant.id, "task-visible-super@example.com", "super_admin")
        admin = _member(s, tenant.id, "task-admin@example.com", "admin")

        client = models.Client(name="Internal")
        s.add(client)
        s.commit()
        task = models.TaskInstance(
            owner_id=visible_super_admin.id,
            submitted_by_id=visible_super_admin.id,
            client_id=client.id,
            client_name="Internal",
            name="Super admin work",
            role="Manager",
            task_type="Admin",
            status="todo",
            segments=[],
        )
        s.add(task)
        s.commit()

        assert task.id not in {row.id for row in main.list_tasks(current_member=admin, db=s)}
        admin = main.update_member_additional_permissions(
            admin.id,
            schemas.MemberAdditionalPermissionsUpdate(
                permissions=[main.PERMISSION_MANAGE_SUPER_ADMINS],
                expected_version=admin.version,
            ),
            current_member=granting_super_admin,
            db=s,
        )
        assert task.id in {row.id for row in main.list_tasks(current_member=admin, db=s)}
    finally:
        s.close()


def test_tracked_time_is_hidden_from_admin_until_explicitly_granted():
    s = database.SessionLocal()
    try:
        tenant = _tenant(s, "tenant_admin_tracked_time")
        super_admin = _member(s, tenant.id, "tracked-super@example.com", "super_admin")
        admin = _member(s, tenant.id, "tracked-admin@example.com", "admin")

        sample = [{"seconds": 1200.0, "tracked_seconds": 900.0, "tracked_hours": 0.25}]
        restricted = main._hide_tracked_time_for_member([dict(sample[0])], admin)
        assert restricted[0]["seconds"] == 1200.0
        assert restricted[0]["tracked_seconds"] is None
        assert restricted[0]["tracked_hours"] is None

        admin = main.update_member_additional_permissions(
            admin.id,
            schemas.MemberAdditionalPermissionsUpdate(
                permissions=[main.PERMISSION_VIEW_TRACKED_TIME],
                expected_version=admin.version,
            ),
            current_member=super_admin,
            db=s,
        )
        visible = main._hide_tracked_time_for_member([dict(sample[0])], admin)
        assert visible[0]["tracked_seconds"] == 900.0
        assert visible[0]["tracked_hours"] == 0.25
    finally:
        s.close()
