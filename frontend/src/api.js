const BASE = "/api";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (body.detail) message = body.detail;
    } catch (e) {
      // response had no JSON body, keep the generic message
    }
    throw new Error(message);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  getMembers: () => request("/members"),
  createMember: (name) => request("/members", { method: "POST", body: JSON.stringify({ name }) }),

  getClients: () => request("/clients"),
  createClient: (name) => request("/clients", { method: "POST", body: JSON.stringify({ name }) }),
  deleteClient: (id) => request(`/clients/${id}`, { method: "DELETE" }),

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
  startTask: (id, ownerId) => request(`/tasks/${id}/start`, { method: "POST", body: JSON.stringify({ owner_id: ownerId }) }),
  pauseTask: (id, endAt) => request(`/tasks/${id}/pause`, { method: "POST", body: JSON.stringify(endAt ? { end_at: endAt } : {}) }),
  submitTask: (id, note) => request(`/tasks/${id}/submit`, { method: "POST", body: JSON.stringify({ note }) }),
  reassignTask: (id, ownerId) =>
    request(`/tasks/${id}/reassign`, { method: "PATCH", body: JSON.stringify({ owner_id: ownerId }) }),
  togglePushed: (id) => request(`/tasks/${id}/toggle-pushed`, { method: "PATCH" }),
  deleteTask: (id) => request(`/tasks/${id}`, { method: "DELETE" }),

  exportRows: (clientId, pushed) => request(`/export?client_id=${clientId}&pushed=${pushed}`),
};

export function exportCsvUrl(clientId, pushed) {
  return `${BASE}/export.csv?client_id=${encodeURIComponent(clientId)}&pushed=${encodeURIComponent(pushed)}`;
}
