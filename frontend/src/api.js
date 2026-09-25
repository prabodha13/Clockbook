const BASE = "/api";
const TOKEN_KEY = "clockbook-token";

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
}

async function request(path, options = {}) {
  const token = getToken();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${BASE}${path}`, { ...options, headers });
  if (res.status === 401 && token) {
    clearToken();
    window.dispatchEvent(new Event("clockbook-session-revoked"));
  }
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body.detail) message = body.detail;
    } catch (e) {
      // response had no JSON body, keep the generic message
    }
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  getAuthStatus: () => request("/auth/status"),
  getServerTime: () => request("/time"),
  getGoogleConnectUrl: () => request("/auth/google/connect-url"),
  getPods: () => request("/pods"),
  createPod: (name) => request("/pods", { method: "POST", body: JSON.stringify({ name }) }),
  deletePod: (id) => request(`/pods/${id}`, { method: "DELETE" }),
  updateMemberPod: (memberId, podId, expectedVersion) => request(`/members/${memberId}/pod`, { method: "PATCH", body: JSON.stringify({ pod_id: podId, expected_version: expectedVersion }) }),
  updateMemberTimezone: (memberId, timezoneName, expectedVersion) => request(`/members/${memberId}/timezone`, { method: "PATCH", body: JSON.stringify({ timezone_name: timezoneName, expected_version: expectedVersion }) }),
  updateMemberInsightsPermission: (memberId, enabled, expectedVersion) => request(`/members/${memberId}/insights-permission`, { method: "PATCH", body: JSON.stringify({ enabled, expected_version: expectedVersion }) }),
  disconnectGoogleCalendar: () => request("/auth/google/disconnect", { method: "POST" }),
  connectSlack: (memberId, slackEmail) => request(`/members/${memberId}/slack`, { method: "PATCH", body: JSON.stringify({ slack_email: slackEmail }) }),
  disconnectSlack: (memberId) => request(`/members/${memberId}/slack/disconnect`, { method: "POST" }),
  testSlack: (memberId) => request(`/members/${memberId}/slack/test`, { method: "POST" }),
  updateNotificationChannel: (memberId, channel) => request(`/members/${memberId}/notification-channel`, { method: "PATCH", body: JSON.stringify({ channel }) }),
  scanCorruptedTasks: () => request("/admin/scan-corrupted-tasks"),
  repairTaskSegments: (taskId) => request(`/tasks/${taskId}/repair-segments`, { method: "POST" }),
  relayNotification: (text) => request("/notifications/relay", { method: "POST", body: JSON.stringify({ text }) }),
  getMeetingNow: () => request("/calendar/meeting-now"),
  getCalendarEvents: (start = null, end = null) => {
    const q = new URLSearchParams();
    if (start) q.set("start", start);
    if (end) q.set("end", end);
    return request(`/calendar/events${q.toString() ? `?${q.toString()}` : ""}`);
  },
  createCalendarEvent: (payload) => request("/calendar/events", { method: "POST", body: JSON.stringify(payload) }),
  updateCalendarEvent: (eventId, payload) => request(`/calendar/events/${eventId}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteCalendarEvent: (eventId) => request(`/calendar/events/${eventId}`, { method: "DELETE" }),
  createQuickMeeting: (payload) => request("/calendar/quick-meeting", { method: "POST", body: JSON.stringify(payload) }),
  getSuggestedTasks: () => request("/calendar/suggested-tasks"),
  dismissSuggestedTask: (eventId) => request(`/calendar/suggested-tasks/${eventId}/dismiss`, { method: "POST" }),
  claimAccount: (payload) => request("/auth/claim", { method: "POST", body: JSON.stringify(payload) }),
  login: (email, password) => request("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => request("/auth/logout", { method: "POST" }),
  getMe: () => request("/auth/me"),
  getWorkspaces: () => request("/auth/workspaces"),
  switchWorkspace: (tenantId) => request(`/auth/switch-workspace/${tenantId}`, { method: "POST" }),
  getWorkspaceBranding: () => request("/workspace/branding"),
  updateWorkspaceBranding: (logoDataUrl) => request("/workspace/branding", { method: "PUT", body: JSON.stringify({ logo_data_url: logoDataUrl || null }) }),
  getPlatformTenants: () => request("/platform/tenants"),
  createPlatformTenant: (name, slug = "") => request("/platform/tenants", { method: "POST", body: JSON.stringify({ name, slug: slug || null }) }),
  getInvitation: (token) => request(`/invitations/${encodeURIComponent(token)}`),
  acceptInvitation: (token, password, name = null) => request(`/invitations/${encodeURIComponent(token)}/accept`, { method: "POST", body: JSON.stringify({ password, name: name || null }) }),
  getTenantInvitations: () => request("/tenant-invitations"),
  createTenantInvitation: (name, email, role = "member") => request("/tenant-invitations", { method: "POST", body: JSON.stringify({ name, email, role }) }),
  regenerateTenantInvitation: (invitationId) => request(`/tenant-invitations/${invitationId}/regenerate`, { method: "POST" }),
  revokeTenantInvitation: (invitationId) => request(`/tenant-invitations/${invitationId}`, { method: "DELETE" }),
  setStaffTourCompleted: (completed = true) => request("/auth/tour", { method: "PATCH", body: JSON.stringify({ completed: !!completed }) }),

  getMembers: () => request("/members"),
  createMember: (name, email, password) =>
    request("/members", { method: "POST", body: JSON.stringify({ name, email, password }) }),
  updateMemberRole: (memberId, role, expectedVersion) =>
    request(`/members/${memberId}/role`, { method: "PATCH", body: JSON.stringify({ role, expected_version: expectedVersion }) }),
  updateMemberCapacity: (memberId, weeklyCapacityHours, capacityEffectiveFrom = null, expectedVersion) =>
    request(`/members/${memberId}/capacity`, { method: "PATCH", body: JSON.stringify({ weekly_capacity_hours: weeklyCapacityHours, capacity_effective_from: capacityEffectiveFrom || null, expected_version: expectedVersion }) }),
  setMemberCredentials: (memberId, email, password) =>
    request(`/members/${memberId}/credentials`, { method: "PATCH", body: JSON.stringify({ email, password }) }),
  deleteMember: (memberId) => request(`/members/${memberId}`, { method: "DELETE" }),

  getClients: () => request("/clients"),
  createClient: (name, code) => request("/clients", { method: "POST", body: JSON.stringify({ name, code }) }),
  importClients: (rows) => request("/clients/import", { method: "POST", body: JSON.stringify({ rows }) }),
  updateClient: (id, name, code, expectedVersion) => request(`/clients/${id}`, { method: "PATCH", body: JSON.stringify({ name, code, expected_version: expectedVersion }) }),
  mergeClients: (keepId, duplicateId) => request(`/clients/${keepId}/merge/${duplicateId}`, { method: "POST" }),
  deleteClient: (id) => request(`/clients/${id}`, { method: "DELETE" }),

  getBankAccounts: () => request("/bank-accounts"),
  createBankAccount: (clientId, name) =>
    request("/bank-accounts", { method: "POST", body: JSON.stringify({ client_id: clientId, name }) }),
  deleteBankAccount: (id) => request(`/bank-accounts/${id}`, { method: "DELETE" }),

  getRoles: () => request("/roles"),
  createRole: (name) => request("/roles", { method: "POST", body: JSON.stringify({ name }) }),
  deleteRole: (id) => request(`/roles/${id}`, { method: "DELETE" }),

  getTaskTypes: () => request("/task-types"),
  createTaskType: (name, isBillable = false) => request("/task-types", { method: "POST", body: JSON.stringify({ name, is_billable: !!isBillable }) }),
  updateTaskTypeBilling: (id, isBillable, expectedVersion) => request(`/task-types/${id}/billing`, { method: "PATCH", body: JSON.stringify({ is_billable: !!isBillable, expected_version: expectedVersion }) }),
  deleteTaskType: (id) => request(`/task-types/${id}`, { method: "DELETE" }),

  getTrackedMetrics: () => request("/tracked-metrics"),
  createTrackedMetric: (name) => request("/tracked-metrics", { method: "POST", body: JSON.stringify({ name }) }),
  deleteTrackedMetric: (id) => request(`/tracked-metrics/${id}`, { method: "DELETE" }),

  getTemplates: () => request("/templates"),
  createTemplate: (field, name, category = "") => request("/templates", { method: "POST", body: JSON.stringify({ field, name, category: category || null }) }),
  updateTemplate: (id, field, name, category = "", expectedVersion) => request(`/templates/${id}`, { method: "PATCH", body: JSON.stringify({ field, name, category: category || null, expected_version: expectedVersion }) }),
  deleteTemplate: (id) => request(`/templates/${id}`, { method: "DELETE" }),
  addTemplateTask: (templateId, task) =>
    request(`/templates/${templateId}/tasks`, { method: "POST", body: JSON.stringify(task) }),
  updateTemplateTask: (templateId, taskId, task, expectedVersion) =>
    request(`/templates/${templateId}/tasks/${taskId}`, { method: "PUT", body: JSON.stringify({ ...task, expected_version: expectedVersion }) }),
  reorderTemplateTasks: (templateId, taskIds) =>
    request(`/templates/${templateId}/tasks-order`, { method: "PUT", body: JSON.stringify({ task_ids: taskIds }) }),
  deleteTemplateTask: (templateId, taskId) =>
    request(`/templates/${templateId}/tasks/${taskId}`, { method: "DELETE" }),

  getInsights: (memberId = "", dateFrom = "", dateTo = "", capacityPodId = "") => {
    const q = new URLSearchParams();
    if (memberId) q.set("member_id", memberId);
    if (dateFrom) q.set("date_from", dateFrom);
    if (dateTo) q.set("date_to", dateTo);
    if (capacityPodId) q.set("capacity_pod_id", capacityPodId);
    return request(`/insights${q.toString() ? `?${q.toString()}` : ""}`);
  },
  getLearningCategories: (includeArchived = false) => request(`/learning/categories${includeArchived ? "?include_archived=true" : ""}`),
  createLearningCategory: (name) => request("/learning/categories", { method: "POST", body: JSON.stringify({ name }) }),
  updateLearningCategory: (id, changes, expectedVersion) => request(`/learning/categories/${id}`, { method: "PATCH", body: JSON.stringify({ ...changes, expected_version: expectedVersion }) }),
  deleteLearningCategory: (id) => request(`/learning/categories/${id}`, { method: "DELETE" }),
  getLearningLibrary: (keyword = "", category = "", letter = "") => {
    const q = new URLSearchParams();
    if (keyword) q.set("keyword", keyword);
    if (category) q.set("category", category);
    if (letter) q.set("letter", letter);
    return request(`/learning/library${q.toString() ? `?${q.toString()}` : ""}`);
  },
  getLearningReport: (dateFrom = "", dateTo = "", personId = "", category = "", keyword = "") => {
    const q = new URLSearchParams();
    if (dateFrom) q.set("date_from", dateFrom);
    if (dateTo) q.set("date_to", dateTo);
    if (personId) q.set("person_id", personId);
    if (category) q.set("category", category);
    if (keyword) q.set("keyword", keyword);
    return request(`/learning/report${q.toString() ? `?${q.toString()}` : ""}`);
  },
  getTasks: () => request("/tasks"),
  createTask: (task) => request("/tasks", { method: "POST", body: JSON.stringify(task) }),
  startTask: (id, startCount, startAt) => {
    const body = {};
    if (startCount != null) body.start_count = startCount;
    if (startAt) body.start_at = startAt;
    return request(`/tasks/${id}/start`, { method: "POST", body: JSON.stringify(body) });
  },
  recoverTaskTime: (id, seconds) =>
    request(`/tasks/${id}/recover-time`, { method: "POST", body: JSON.stringify({ seconds }) }),
  createHelpEvent: (colleagueId, direction, seconds, source, adjusted = false, context = "", inactivityEventId = null) =>
    request("/help-events", { method: "POST", body: JSON.stringify({ colleague_id: colleagueId, direction, seconds, source, adjusted, context, inactivity_event_id: inactivityEventId }) }),
  startAdHocMeeting: (colleagueId = null) =>
    request("/ad-hoc-meetings/start", { method: "POST", body: JSON.stringify({ colleague_id: colleagueId }) }),
  finishAdHocMeeting: (taskId, colleagueId, interaction, context) =>
    request(`/ad-hoc-meetings/${taskId}/finish`, { method: "POST", body: JSON.stringify({ colleague_id: colleagueId, interaction, context }) }),
  getHelpEventsSummary: () => request("/help-events/summary"),
  getHelpEventsDetail: () => request("/help-events/detail"),
  createInactivityEvent: (kind, startedAt, endedAt, taskId = null) =>
    request("/inactivity-events", { method: "POST", body: JSON.stringify({ kind, started_at: startedAt, ended_at: endedAt, task_id: taskId }) }),
  getInactivityAuditStatus: () => request("/inactivity-events/status"),
  getAuditActivitySummary: (dateFrom = "", dateTo = "") => { const q = new URLSearchParams(); if (dateFrom) q.set("date_from", dateFrom); if (dateTo) q.set("date_to", dateTo); return request(`/audit/activity-summary${q.toString() ? `?${q.toString()}` : ""}`); },
  sendPresenceHeartbeat: () => request("/audit/presence-heartbeat", { method: "POST" }),
  getKarbonReconciliation: (memberId, dateFrom, dateTo) => { const q = new URLSearchParams(); if (memberId) q.set("member_id", memberId); if (dateFrom) q.set("date_from", dateFrom); if (dateTo) q.set("date_to", dateTo); return request(`/karbon/reconciliation?${q.toString()}`); },
  saveKarbonReconciliationNote: (memberId, date, note) => request("/karbon/reconciliation/note", { method: "PUT", body: JSON.stringify({ member_id: memberId, date, note }) }),
  getIntegrationStatus: () => request("/integrations/status"),
  getKarbonIntegration: () => request("/integrations/karbon"),
  saveKarbonIntegration: (applicationId, accessKey, expectedVersion = null) => request("/integrations/karbon", { method: "PUT", body: JSON.stringify({ application_id: applicationId, access_key: accessKey, expected_version: expectedVersion }) }),
  testKarbonIntegration: () => request("/integrations/karbon/test", { method: "POST" }),
  disconnectKarbonIntegration: (expectedVersion = null) => request(`/integrations/karbon${expectedVersion == null ? "" : `?expected_version=${encodeURIComponent(expectedVersion)}`}`, { method: "DELETE" }),
  getCalamariIntegration: () => request("/integrations/calamari"),
  saveCalamariIntegration: (tenant, apiKey, expectedVersion = null) => request("/integrations/calamari", { method: "PUT", body: JSON.stringify({ tenant, api_key: apiKey, expected_version: expectedVersion }) }),
  testCalamariIntegration: () => request("/integrations/calamari/test", { method: "POST" }),
  disconnectCalamariIntegration: (expectedVersion = null) => request(`/integrations/calamari${expectedVersion == null ? "" : `?expected_version=${encodeURIComponent(expectedVersion)}`}`, { method: "DELETE" }),
  setInactivityAuditStatus: (enabled) => request("/inactivity-events/status", { method: "PUT", body: JSON.stringify({ enabled }) }),
  getInactivityEvents: (dateFrom = "", dateTo = "") => {
    const params = new URLSearchParams();
    if (dateFrom) params.set("date_from", dateFrom);
    if (dateTo) params.set("date_to", dateTo);
    const qs = params.toString();
    return request(`/inactivity-events${qs ? `?${qs}` : ""}`);
  },
  sendHeartbeat: (id) => request(`/tasks/${id}/heartbeat`, { method: "POST" }),
  pauseTask: (id, endAt) => request(`/tasks/${id}/pause`, { method: "POST", body: JSON.stringify(endAt ? { end_at: endAt } : {}) }),
  resetTask: (id) => request(`/tasks/${id}/reset`, { method: "POST" }),
  getExportRows: (clientId, pushed, dateFrom, dateTo, submittedBy) =>
    request(`/export?${exportQueryParams(clientId, pushed, dateFrom, dateTo, submittedBy)}`),
  submitTask: (id, note, endCount, adjustedSeconds, role, taskType, clientId, period, learning = null) =>
    request(`/tasks/${id}/submit`, {
      method: "POST",
      body: JSON.stringify({
        note, client_id: clientId || null, end_count: endCount != null ? endCount : null,
        adjusted_seconds: adjustedSeconds != null ? adjustedSeconds : null,
        role: role != null ? role : null, task_type: taskType != null ? taskType : null,
        period_type: period?.type || null, period_year: period?.year || null, period_number: period?.number || null,
        period_start: period?.start || null, period_end: period?.end || null,
        learning_category: learning?.category || null, learning_topic: learning?.topic || null,
        what_i_learned: learning?.whatILearned || null,
        tdm_references: learning?.tdmReferences || [], article_references: learning?.articleReferences || [],
      }),
    }),
  reassignTask: (id, ownerId) =>
    request(`/tasks/${id}/reassign`, { method: "PATCH", body: JSON.stringify({ owner_id: ownerId }) }),
  togglePushed: (id) => request(`/tasks/${id}/toggle-pushed`, { method: "PATCH" }),
  deleteTask: (id) => request(`/tasks/${id}`, { method: "DELETE" }),
};

function exportQueryParams(clientId, pushed, dateFrom, dateTo, submittedBy) {
  const params = new URLSearchParams({ client_id: clientId, pushed });
  if (dateFrom) params.set("date_from", dateFrom);
  if (dateTo) params.set("date_to", dateTo);
  if (submittedBy) params.set("submitted_by", submittedBy);
  return params.toString();
}

export function exportCsvUrl(clientId, pushed, dateFrom, dateTo, submittedBy) {
  return `${BASE}/export.csv?${exportQueryParams(clientId, pushed, dateFrom, dateTo, submittedBy)}`;
}

// Export now requires a login, and a plain browser navigation cannot carry the
// Authorization header, so the CSV is fetched here and turned into a real download
// instead of just pointing the browser at the URL.
export async function downloadCsvFile(clientId, pushed, dateFrom, dateTo, filename, submittedBy) {
  const token = getToken();
  const res = await fetch(exportCsvUrl(clientId, pushed, dateFrom, dateTo, submittedBy), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export async function fetchCsvText(clientId, pushed, dateFrom, dateTo, submittedBy) {
  const token = getToken();
  const res = await fetch(exportCsvUrl(clientId, pushed, dateFrom, dateTo, submittedBy), {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  return res.text();
}
