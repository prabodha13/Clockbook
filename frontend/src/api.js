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
  createMember: (name, role, createdBy) => request("/members", {
  method: "POST",
  body: JSON.stringify({
    name,
    role,
    created_by: createdBy || null
  })
}),
  updateMemberRole: (memberId, role, actorId) =>
    request(`/members/${memberId}/role`, { method: "PATCH", body: JSON.stringify({ role, actor_id: actorId }) }),

  getClients: () => request("/clients"),
  createClient: (name) => request("/clients", { method: "POST", body: JSON.stringify({ name }) }),
  deleteClient: (id, actorId) => request(`/clients/${id}?actor_id=${encodeURIComponent(actorId)}`, { method: "DELETE" }),

  getTemplates: () => request("/templates"),
  createTemplate: (field, name, actorId) => request("/templates", { method: "POST", body: JSON.stringify({ field, name, actor_id: actorId }) }),
  deleteTemplate: (id, actorId) => request(`/templates/${id}?actor_id=${encodeURIComponent(actorId)}`, { method: "DELETE" }),
  addTemplateTask: (templateId, task, actorId) =>
    request(`/templates/${templateId}/tasks`, { method: "POST", body: JSON.stringify({ ...task, actor_id: actorId }) }),
  updateTemplateTask: (templateId, taskId, task, actorId) =>
    request(`/templates/${templateId}/tasks/${taskId}`, { method: "PUT", body: JSON.stringify({ ...task, actor_id: actorId }) }),
  deleteTemplateTask: (templateId, taskId, actorId) =>
    request(`/templates/${templateId}/tasks/${taskId}?actor_id=${encodeURIComponent(actorId)}`, { method: "DELETE" }),

  getTasks: () => request("/tasks"),
  createTask: (task) => request("/tasks", { method: "POST", body: JSON.stringify(task) }),
  startTask: (id, ownerId) => request(`/tasks/${id}/start`, { method: "POST", body: JSON.stringify({ owner_id: ownerId }) }),
  pauseTask: (id, endAt) => request(`/tasks/${id}/pause`, { method: "POST", body: JSON.stringify(endAt ? { end_at: endAt } : {}) }),
  submitTask: (id, note) => request(`/tasks/${id}/submit`, { method: "POST", body: JSON.stringify({ note }) }),
  reassignTask: (id, ownerId, actorId) =>
    request(`/tasks/${id}/reassign`, { method: "PATCH", body: JSON.stringify({ owner_id: ownerId, actor_id: actorId }) }),
  togglePushed: (id) => request(`/tasks/${id}/toggle-pushed`, { method: "PATCH" }),
  deleteTask: (id, actorId) => request(`/tasks/${id}?actor_id=${encodeURIComponent(actorId)}`, { method: "DELETE" }),

  exportRows: (clientId, pushed) => request(`/export?client_id=${clientId}&pushed=${pushed}`),
};

export function exportCsvUrl(clientId, pushed, dateFrom, dateTo) {
  const params = new URLSearchParams({ client_id: clientId, pushed });
  if (dateFrom) params.set("date_from", dateFrom);
  if (dateTo) params.set("date_to", dateTo);
  return `${BASE}/export.csv?${params.toString()}`;
}
