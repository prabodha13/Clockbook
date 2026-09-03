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
  disconnectGoogleCalendar: () => request("/auth/google/disconnect", { method: "POST" }),
  getMeetingNow: () => request("/calendar/meeting-now"),
  claimAccount: (payload) => request("/auth/claim", { method: "POST", body: JSON.stringify(payload) }),
  login: (email, password) => request("/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => request("/auth/logout", { method: "POST" }),
  getMe: () => request("/auth/me"),

  getMembers: () => request("/members"),
  createMember: (name, email, password) =>
    request("/members", { method: "POST", body: JSON.stringify({ name, email, password }) }),
  updateMemberRole: (memberId, role) =>
    request(`/members/${memberId}/role`, { method: "PATCH", body: JSON.stringify({ role }) }),
  setMemberCredentials: (memberId, email, password) =>
    request(`/members/${memberId}/credentials`, { method: "PATCH", body: JSON.stringify({ email, password }) }),
  deleteMember: (memberId) => request(`/members/${memberId}`, { method: "DELETE" }),

  getClients: () => request("/clients"),
  createClient: (name) => request("/clients", { method: "POST", body: JSON.stringify({ name }) }),
  deleteClient: (id) => request(`/clients/${id}`, { method: "DELETE" }),

  getBankAccounts: () => request("/bank-accounts"),
  createBankAccount: (clientId, name) =>
    request("/bank-accounts", { method: "POST", body: JSON.stringify({ client_id: clientId, name }) }),
  deleteBankAccount: (id) => request(`/bank-accounts/${id}`, { method: "DELETE" }),

  getRoles: () => request("/roles"),
  createRole: (name) => request("/roles", { method: "POST", body: JSON.stringify({ name }) }),
  deleteRole: (id) => request(`/roles/${id}`, { method: "DELETE" }),

  getTaskTypes: () => request("/task-types"),
  createTaskType: (name) => request("/task-types", { method: "POST", body: JSON.stringify({ name }) }),
  deleteTaskType: (id) => request(`/task-types/${id}`, { method: "DELETE" }),

  getTrackedMetrics: () => request("/tracked-metrics"),
  createTrackedMetric: (name) => request("/tracked-metrics", { method: "POST", body: JSON.stringify({ name }) }),
  deleteTrackedMetric: (id) => request(`/tracked-metrics/${id}`, { method: "DELETE" }),

  getTemplates: () => request("/templates"),
  createTemplate: (field, name) => request("/templates", { method: "POST", body: JSON.stringify({ field, name }) }),
  deleteTemplate: (id) => request(`/templates/${id}`, { method: "DELETE" }),
  addTemplateTask: (templateId, task) =>
    request(`/templates/${templateId}/tasks`, { method: "POST", body: JSON.stringify(task) }),
  updateTemplateTask: (templateId, taskId, task) =>
    request(`/templates/${templateId}/tasks/${taskId}`, { method: "PUT", body: JSON.stringify(task) }),
  deleteTemplateTask: (templateId, taskId) =>
    request(`/templates/${templateId}/tasks/${taskId}`, { method: "DELETE" }),

  getTasks: () => request("/tasks"),
  createTask: (task) => request("/tasks", { method: "POST", body: JSON.stringify(task) }),
  startTask: (id, startCount) =>
    request(`/tasks/${id}/start`, { method: "POST", body: JSON.stringify(startCount != null ? { start_count: startCount } : {}) }),
  pauseTask: (id, endAt) => request(`/tasks/${id}/pause`, { method: "POST", body: JSON.stringify(endAt ? { end_at: endAt } : {}) }),
  resetTask: (id) => request(`/tasks/${id}/reset`, { method: "POST" }),
  getExportRows: (clientId, pushed, dateFrom, dateTo, submittedBy) =>
    request(`/export?${exportQueryParams(clientId, pushed, dateFrom, dateTo, submittedBy)}`),
  submitTask: (id, note, endCount, adjustedSeconds) =>
    request(`/tasks/${id}/submit`, {
      method: "POST",
      body: JSON.stringify({
        note, end_count: endCount != null ? endCount : null,
        adjusted_seconds: adjustedSeconds != null ? adjustedSeconds : null,
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
