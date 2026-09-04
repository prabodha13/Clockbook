import { useState, useEffect, useMemo, useCallback, useRef, Fragment } from "react";
import {
  Clock, Play, Pause, Plus, X, Trash2, Download, Copy,
  ChevronDown, Building2, LayoutDashboard, ListTree, FileSpreadsheet, Users,
  CheckCircle2, StickyNote, ClipboardList, LogOut, Settings, RotateCcw,
  Calendar as CalendarIcon, Video, Edit3, Ban,
} from "lucide-react";
import { api, downloadCsvFile, fetchCsvText, getToken, setToken, clearToken } from "./api.js";

const MEMBER_TINTS = ["#245C43", "#B5590F", "#5B6660", "#5C4A8C", "#8C2F3A", "#2E5C7A"];

function isAdminRole(role) {
  return role === "admin" || role === "super_admin";
}

function elapsedSeconds(task, nowMs) {
  let total = 0;
  for (const seg of task.segments || []) {
    const start = new Date(seg.start).getTime();
    const end = seg.end ? new Date(seg.end).getTime() : nowMs;
    total += Math.max(0, end - start) / 1000;
  }
  return total;
}

function formatHMS(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

function formatHM(totalSeconds) {
  const totalMinutes = Math.round(totalSeconds / 60);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  if (h <= 0) return `${m}m`;
  return `${h}h ${m}m`;
}

function decimalHours(totalSeconds) {
  return (totalSeconds / 3600).toFixed(2);
}

function formatDate(iso) {
  if (!iso) return "none";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function isToday(iso) {
  if (!iso) return false;
  const d = new Date(iso);
  const n = new Date();
  return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
}

// Start of the Monday to Sunday week containing d, at local midnight
function startOfWeek(d) {
  const date = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const day = date.getDay(); // 0 = Sunday
  const diff = day === 0 ? -6 : 1 - day;
  date.setDate(date.getDate() + diff);
  return date;
}

function localDayStart(y, m, d) {
  return new Date(y, m, d, 0, 0, 0, 0);
}
function localDayEnd(y, m, d) {
  return new Date(y, m, d, 23, 59, 59, 999);
}

function dateRangeForPreset(preset) {
  const now = new Date();
  let start, end;
  if (preset === "this_week") {
    start = startOfWeek(now);
    end = new Date(start); end.setDate(end.getDate() + 6);
  } else if (preset === "last_week") {
    start = startOfWeek(now); start.setDate(start.getDate() - 7);
    end = new Date(start); end.setDate(end.getDate() + 6);
  } else if (preset === "this_month") {
    start = new Date(now.getFullYear(), now.getMonth(), 1);
    end = new Date(now.getFullYear(), now.getMonth() + 1, 0);
  } else if (preset === "last_month") {
    start = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    end = new Date(now.getFullYear(), now.getMonth(), 0);
  } else {
    return null;
  }
  const from = localDayStart(start.getFullYear(), start.getMonth(), start.getDate());
  const to = localDayEnd(end.getFullYear(), end.getMonth(), end.getDate());
  return {
    fromIso: from.toISOString(),
    toIso: to.toISOString(),
    label: `${from.toLocaleDateString(undefined, { month: "short", day: "numeric" })} to ${to.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`,
  };
}

// Custom range comes from <input type="date"> as plain YYYY-MM-DD strings with no timezone.
// Adding the time portion without a Z tells the browser to read it as local time, so the
// day boundaries line up with what the person actually picked rather than shifting with UTC.
function dateRangeForCustom(fromStr, toStr) {
  if (!fromStr || !toStr) return null;
  const from = new Date(`${fromStr}T00:00:00`);
  const to = new Date(`${toStr}T23:59:59.999`);
  return {
    fromIso: from.toISOString(),
    toIso: to.toISOString(),
    label: `${from.toLocaleDateString(undefined, { month: "short", day: "numeric" })} to ${to.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`,
  };
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    return false;
  }
}

function Avatar({ member, size }) {
  const tint = MEMBER_TINTS[member.color_idx % MEMBER_TINTS.length];
  const parts = member.name.trim().split(/\s+/);
  const initials = parts.length > 1
    ? (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
    : member.name.trim().slice(0, 2).toUpperCase();
  return (
    <div className="cb-avatar" style={{ background: tint, width: size || 24, height: size || 24, fontSize: size ? size * 0.42 : 11 }}>
      {initials}
    </div>
  );
}

function StatusBadge({ status }) {
  if (status === "running") return <span className="cb-badge cb-badge-running"><Play size={10} />Running</span>;
  if (status === "paused") return <span className="cb-badge cb-badge-paused"><Pause size={10} />Paused</span>;
  if (status === "submitted") return <span className="cb-badge cb-badge-submitted"><CheckCircle2 size={10} />Submitted</span>;
  return <span className="cb-badge cb-badge-todo">To do</span>;
}

function LoadingScreen() {
  return (
    <div className="cb-center-screen">
      <div style={{ color: "var(--ink-soft)", fontSize: 14 }}>Loading Clockbook</div>
    </div>
  );
}

function LoginScreen({ onLogin }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await onLogin(email, password);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <div className="cb-center-screen">
      <div className="cb-welcome">
        <div className="cb-welcome-mark"><Clock size={22} /></div>
        <div className="cb-welcome-title cb-serif">Log in to Clockbook</div>
        <div className="cb-welcome-sub">Time tracking built around clients and tasks, with a clean handoff into Karbon.</div>
        <form onSubmit={submit}>
          <div className="cb-field" style={{ textAlign: "left" }}>
            <label className="cb-label">Email</label>
            <input className="cb-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required />
          </div>
          <div className="cb-field" style={{ textAlign: "left" }}>
            <label className="cb-label">Password</label>
            <input className="cb-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
          {error && <div className="cb-error" style={{ marginBottom: 14 }}>{error}</div>}
          <button type="submit" className="cb-btn cb-btn-primary" style={{ width: "100%", justifyContent: "center" }} disabled={busy}>
            Log in
          </button>
        </form>
      </div>
    </div>
  );
}

function ClaimScreen({ unclaimed, onClaim }) {
  const [mode, setMode] = useState(unclaimed.length ? "existing" : "new");
  const [memberId, setMemberId] = useState(unclaimed[0] ? unclaimed[0].id : "");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await onClaim({
        memberId: mode === "existing" ? memberId : null,
        name: mode === "new" ? name : null,
        email, password,
      });
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <div className="cb-center-screen">
      <div className="cb-welcome" style={{ maxWidth: 420 }}>
        <div className="cb-welcome-mark"><Clock size={22} /></div>
        <div className="cb-welcome-title cb-serif">Set up your login</div>
        <div className="cb-welcome-sub">
          This workspace has data already but no passwords yet. The first person to set one up becomes the admin.
        </div>
        <form onSubmit={submit}>
          {unclaimed.length > 0 && (
            <div className="cb-tabs" style={{ marginBottom: 14, width: "fit-content", marginLeft: "auto", marginRight: "auto" }}>
              <button type="button" className={`cb-tab ${mode === "existing" ? "active" : ""}`} onClick={() => setMode("existing")}>I am already listed</button>
              <button type="button" className={`cb-tab ${mode === "new" ? "active" : ""}`} onClick={() => setMode("new")}>I am new</button>
            </div>
          )}
          {mode === "existing" ? (
            <div className="cb-field" style={{ textAlign: "left" }}>
              <label className="cb-label">Which one are you?</label>
              <select className="cb-select" value={memberId} onChange={(e) => setMemberId(e.target.value)}>
                {unclaimed.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
            </div>
          ) : (
            <div className="cb-field" style={{ textAlign: "left" }}>
              <label className="cb-label">Your name</label>
              <input className="cb-input" value={name} onChange={(e) => setName(e.target.value)} required />
            </div>
          )}
          <div className="cb-field" style={{ textAlign: "left" }}>
            <label className="cb-label">Email</label>
            <input className="cb-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div className="cb-field" style={{ textAlign: "left" }}>
            <label className="cb-label">Choose a password</label>
            <input className="cb-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={8} required />
          </div>
          {error && <div className="cb-error" style={{ marginBottom: 14 }}>{error}</div>}
          <button type="submit" className="cb-btn cb-btn-primary" style={{ width: "100%", justifyContent: "center" }} disabled={busy}>
            Set up and continue
          </button>
        </form>
      </div>
    </div>
  );
}

function Sidebar({ view, setView, isAdmin }) {
  const items = [
    { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
    { id: "templates", label: "Templates", icon: ListTree },
    { id: "clients", label: "Clients", icon: Building2 },
    { id: "calendar", label: "Calendar", icon: CalendarIcon },
    { id: "export", label: "Export", icon: FileSpreadsheet },
    { id: "staff", label: "Staff", icon: Users },
    ...(isAdmin ? [{ id: "settings", label: "Settings", icon: Settings }] : []),
  ];
  return (
    <div className="cb-sidebar">
      <div className="cb-brand">
        <div className="cb-brand-mark"><Clock size={15} /></div>
        <div className="cb-brand-name cb-serif">Clockbook</div>
      </div>
      <div className="cb-nav">
        {items.map((it) => (
          <button key={it.id} className={`cb-nav-item ${view === it.id ? "active" : ""}`} onClick={() => setView(it.id)}>
            <it.icon size={16} />
            {it.label}
          </button>
        ))}
      </div>
      <div className="cb-sidebar-foot">Time tracked here stays in your firm's own database until you export it.</div>
    </div>
  );
}

function TopBar({ currentUser, onLogout, pinnedTask, now, onPause, onResume, onComplete }) {
  const isAdmin = isAdminRole(currentUser.role);
  const isPaused = pinnedTask && pinnedTask.status === "paused";
  const elapsed = pinnedTask ? elapsedSeconds(pinnedTask, now) : 0;
  return (
    <div className="cb-topbar">
      {pinnedTask ? (
        <div className={`cb-tracking${isPaused ? " paused" : ""}`}>
          <div className="cb-tracking-dot" />
          <div className="cb-tracking-text">
            <div className="cb-tracking-label">{isPaused ? "Paused" : "Now tracking"}</div>
            <div className="cb-tracking-name">{pinnedTask.client_name}: {pinnedTask.name}</div>
          </div>
          <div className="cb-tracking-time cb-mono">{formatHMS(elapsed)}</div>
          <div className="cb-tracking-actions">
            {isPaused ? (
              <button className="cb-btn cb-btn-sm" onClick={onResume}><Play size={13} />Resume</button>
            ) : (
              <button className="cb-btn cb-btn-sm" onClick={onPause}><Pause size={13} />Pause</button>
            )}
            <button className="cb-btn cb-btn-sm cb-btn-primary" onClick={onComplete}><CheckCircle2 size={13} />Complete</button>
          </div>
        </div>
      ) : (
        <div style={{ color: "var(--ink-faint)", fontSize: 13 }}>No timer running</div>
      )}
      <div className="cb-user-menu" style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div className="cb-user-btn" style={{ cursor: "default" }}>
          <Avatar member={currentUser} />
          {currentUser.name}
          {isAdmin && <span className="cb-role-badge">{currentUser.role === "super_admin" ? "Super Admin" : "Admin"}</span>}
        </div>
        <button className="cb-btn cb-btn-sm cb-btn-ghost" onClick={onLogout} title="Log out">
          <LogOut size={13} />Log out
        </button>
      </div>
    </div>
  );
}

function TaskRow({ task, now, currentUser, members, onStart, onPause, onComplete, onDelete, onReassign, onReset, hideClient }) {
  const isMine = task.owner_id === currentUser.id;
  const isAdmin = isAdminRole(currentUser.role);
  const canReset = isMine || isAdmin;
  const elapsed = elapsedSeconds(task, now);
  const owner = members.find((m) => m.id === task.owner_id);
  return (
    <div className="cb-row">
      <div className="cb-row-main">
        {!hideClient && <div className="cb-row-client"><Building2 size={11} />{task.client_name}</div>}
        <div className="cb-row-task">{taskHeading(task.name, task.bank_account_name, task.pay_period_type, task.pay_period_number)}</div>
        <div className="cb-row-meta">
          {task.role && <span>{task.role}</span>}
          {task.task_type && <span>{task.task_type}</span>}
          {task.source_template_name && <span>Template: {task.source_template_name}</span>}
          <span>
            Owner:{" "}
            {isAdmin ? (
              <select
                className="cb-owner-select"
                value={task.owner_id || ""}
                onChange={(e) => onReassign(task.id, e.target.value)}
                disabled={task.status === "running"}
              >
                {members.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
            ) : (
              owner ? owner.name : "unassigned"
            )}
          </span>
        </div>
      </div>
      <StatusBadge status={task.status} />
      <div className="cb-row-time cb-mono">{elapsed > 0 ? formatHM(elapsed) : "0m"}</div>
      <div className="cb-row-actions">
        {(task.status === "todo" || task.status === "paused") && (
          <button className="cb-icon-btn" title={isMine ? "Start" : `Owned by ${owner ? owner.name : "someone else"}`} disabled={!isMine} onClick={() => onStart(task)}>
            <Play size={14} />
          </button>
        )}
        {task.status === "running" && (
          <button className="cb-icon-btn" title="Pause" disabled={!isMine} onClick={() => onPause(task.id)}>
            <Pause size={14} />
          </button>
        )}
        {(task.status === "running" || task.status === "paused") && canReset && (
          <button
            className="cb-icon-btn"
            title="Reset time"
            onClick={() => {
              if (window.confirm(`Reset "${task.client_name}: ${task.name}"? This clears ${formatHM(elapsed)} of tracked time back to zero and moves it back to To do.`)) {
                onReset(task.id);
              }
            }}
          >
            <RotateCcw size={14} />
          </button>
        )}
        {(task.status === "running" || task.status === "paused") && (
          <button className="cb-btn cb-btn-sm cb-btn-primary" disabled={!isMine} onClick={() => onComplete(task)}>
            Complete
          </button>
        )}
        {(isAdmin || (isMine && task.status !== "submitted")) && (
          <button
            className="cb-icon-btn cb-btn-danger"
            title="Delete"
            onClick={() => {
              if (task.status === "todo" || window.confirm(`This task has ${formatHM(elapsed)} tracked on it. Delete it anyway?`)) {
                onDelete(task.id);
              }
            }}
          >
            <Trash2 size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

// If the person picked a specific account for a bank-required template task, use that,
// otherwise auto-fill it when the client only has one account to begin with, this is used
// consistently for what is shown, what counts as complete, and what actually gets submitted,
// so those three things can never disagree with each other.
function resolvedBankAccountId(row, taskId, clientAccounts) {
  if (row.bankAccountByTaskId[taskId]) return row.bankAccountByTaskId[taskId];
  if (clientAccounts.length === 1) return clientAccounts[0].id;
  return "";
}

function SuggestedTasksReviewModal({ suggestions, clients, templates, roles, taskTypes, bankAccounts, onClose, onDone, onCreateTasks }) {
  const [rows, setRows] = useState(() =>
    suggestions.map((s) => ({
      suggestion: s,
      clientId: "",
      role: "",
      useTemplate: null, // null until chosen, then true or false
      templateId: "",
      selectedTaskIds: [], // which specific tasks within the chosen template, none pre-checked
      taskType: "",
      bankAccountByTaskId: {},
    }))
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function updateRow(index, patch) {
    setRows((prev) => prev.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  function templateTasksFor(row) {
    if (!row.useTemplate || !row.templateId) return [];
    const tpl = templates.find((t) => t.id === row.templateId);
    return tpl ? tpl.tasks : [];
  }

  function selectedTemplateTasksFor(row) {
    return templateTasksFor(row).filter((t) => row.selectedTaskIds.includes(t.id));
  }

  function bankTasksFor(row) {
    return selectedTemplateTasksFor(row).filter((t) => t.requires_bank_account);
  }

  function toggleTemplateTaskId(index, taskId) {
    setRows((prev) => prev.map((r, i) => {
      if (i !== index) return r;
      const isSelected = r.selectedTaskIds.includes(taskId);
      const nextBankAccountByTaskId = { ...r.bankAccountByTaskId };
      if (isSelected) delete nextBankAccountByTaskId[taskId];
      return {
        ...r,
        selectedTaskIds: isSelected ? r.selectedTaskIds.filter((id) => id !== taskId) : [...r.selectedTaskIds, taskId],
        bankAccountByTaskId: nextBankAccountByTaskId,
      };
    }));
  }

  function isRowComplete(row) {
    if (!row.clientId || !row.role || row.useTemplate === null) return false;
    if (row.useTemplate === false) return !!row.taskType;
    if (!row.templateId || row.selectedTaskIds.length === 0) return false;
    const clientAccounts = bankAccounts.filter((a) => a.client_id === row.clientId);
    return bankTasksFor(row).every((t) => resolvedBankAccountId(row, t.id, clientAccounts));
  }

  const allComplete = rows.length > 0 && rows.every(isRowComplete);

  async function handleCreate() {
    setError("");
    if (!allComplete) {
      setError("Finish classifying every item before creating tasks, each one needs at least a client, a role, a template choice, and at least one task selected from it.");
      return;
    }
    setBusy(true);
    try {
      const payloads = [];
      for (const row of rows) {
        const client = clients.find((c) => c.id === row.clientId);
        if (row.useTemplate === false) {
          payloads.push({
            client_id: row.clientId, client_name: client.name, name: row.suggestion.summary,
            role: row.role, task_type: row.taskType,
            source_calendar_event_id: row.suggestion.id,
          });
        } else {
          const clientAccounts = bankAccounts.filter((a) => a.client_id === row.clientId);
          const tpl = templates.find((t) => t.id === row.templateId);
          for (const tt of selectedTemplateTasksFor(row)) {
            const accountId = tt.requires_bank_account ? resolvedBankAccountId(row, tt.id, clientAccounts) : null;
            const account = accountId ? bankAccounts.find((a) => a.id === accountId) : null;
            payloads.push({
              client_id: row.clientId, client_name: client.name, name: tt.name,
              role: row.role, task_type: tt.task_type,
              bank_account_id: account ? account.id : null,
              bank_account_name: account ? account.name : "",
              tracks_number_label: tt.tracks_number_label || "",
              source_calendar_event_id: row.suggestion.id,
              source_template_name: tpl ? tpl.name : null,
            });
          }
        }
      }
      await onCreateTasks(payloads);
      await onDone();
    } catch (err) {
      setError(err.message || "Could not create tasks");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="cb-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cb-modal" style={{ maxWidth: 720 }}>
        <div className="cb-modal-head">
          <div className="cb-modal-title">Add to To Do</div>
          <button className="cb-icon-btn" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="cb-modal-body">
          {rows.map((row, i) => {
            const bankTasks = bankTasksFor(row);
            const templateTasks = templateTasksFor(row);
            const clientAccounts = row.clientId ? bankAccounts.filter((a) => a.client_id === row.clientId) : [];
            return (
              <div key={row.suggestion.id} style={{ padding: "14px 0", borderBottom: i < rows.length - 1 ? "1px solid var(--line)" : "none" }}>
                <div style={{ fontWeight: 500, marginBottom: 10 }}>{row.suggestion.summary}</div>
                <div className="cb-field-row">
                  <div className="cb-field">
                    <label className="cb-label">Client</label>
                    <SearchableSelect
                      options={clients} value={row.clientId} onChange={(v) => updateRow(i, { clientId: v })}
                      placeholder="Search clients..." getLabel={(c) => c.name}
                    />
                  </div>
                  <div className="cb-field">
                    <label className="cb-label">Role</label>
                    <select className="cb-select" value={row.role} onChange={(e) => updateRow(i, { role: e.target.value })}>
                      <option value="">Select a role</option>
                      {roles.map((r) => <option key={r.id} value={r.name}>{r.name}</option>)}
                    </select>
                  </div>
                </div>
                <div className="cb-field">
                  <label className="cb-label">Does this use a standard template?</label>
                  <div className="cb-tabs" style={{ width: "fit-content" }}>
                    <button type="button" className={`cb-tab ${row.useTemplate === false ? "active" : ""}`} onClick={() => updateRow(i, { useTemplate: false })}>No, single task</button>
                    <button type="button" className={`cb-tab ${row.useTemplate === true ? "active" : ""}`} onClick={() => updateRow(i, { useTemplate: true, taskType: "" })}>Yes, use a template</button>
                  </div>
                </div>
                {row.useTemplate === false && (
                  <div className="cb-field">
                    <label className="cb-label">Task type</label>
                    <select className="cb-select" value={row.taskType} onChange={(e) => updateRow(i, { taskType: e.target.value })}>
                      <option value="">Select a task type</option>
                      {taskTypes.map((t) => <option key={t.id} value={t.name}>{t.name}</option>)}
                    </select>
                  </div>
                )}
                {row.useTemplate === true && (
                  <>
                    <div className="cb-field">
                      <label className="cb-label">Template</label>
                      <SearchableSelect
                        options={templates} value={row.templateId}
                        onChange={(v) => updateRow(i, { templateId: v, selectedTaskIds: [], bankAccountByTaskId: {} })}
                        placeholder="Search templates..." getLabel={(t) => t.name} getSecondary={(t) => t.field}
                      />
                    </div>
                    {row.templateId && (
                      <div className="cb-field">
                        <label className="cb-label">Which tasks from this template?</label>
                        <div className="cb-checklist">
                          {templateTasks.length === 0 && (
                            <div className="cb-hint" style={{ padding: 10 }}>This template has no tasks yet.</div>
                          )}
                          {templateTasks.map((t) => (
                            <label key={t.id} className="cb-checklist-item">
                              <input
                                type="checkbox" className="cb-checkbox" checked={row.selectedTaskIds.includes(t.id)}
                                onChange={() => toggleTemplateTaskId(i, t.id)}
                              />
                              <div>
                                <div className="cb-checklist-name">{t.name}</div>
                                <div className="cb-checklist-meta">
                                  {t.task_type || "no task type"}
                                  {t.requires_bank_account ? " \u00b7 needs a bank account" : ""}
                                </div>
                              </div>
                            </label>
                          ))}
                        </div>
                      </div>
                    )}
                    {bankTasks.length > 0 && (
                      <div className="cb-hint" style={{ marginBottom: 6 }}>
                        {!row.clientId ? "Pick a client above to choose a bank account." : null}
                      </div>
                    )}
                    {row.clientId && bankTasks.map((tt) => (
                      <div className="cb-field" key={tt.id}>
                        <label className="cb-label">Bank account for "{tt.name}"</label>
                        <select
                          className="cb-select"
                          value={resolvedBankAccountId(row, tt.id, clientAccounts)}
                          onChange={(e) => updateRow(i, { bankAccountByTaskId: { ...row.bankAccountByTaskId, [tt.id]: e.target.value } })}
                        >
                          <option value="">Select a bank account</option>
                          {clientAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                        </select>
                      </div>
                    ))}
                  </>
                )}
              </div>
            );
          })}
          {error && <div className="cb-error">{error}</div>}
        </div>
        <div className="cb-modal-foot">
          <button className="cb-btn cb-btn-ghost" onClick={onClose}>Cancel</button>
          <button className="cb-btn cb-btn-primary" disabled={busy} onClick={handleCreate}>
            {busy ? "Creating..." : "Create Tasks"}
          </button>
        </div>
      </div>
    </div>
  );
}

function SuggestedTasksSection({ currentUser, clients, templates, roles, taskTypes, bankAccounts, onCreateTasks }) {
  const [suggestions, setSuggestions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [showReview, setShowReview] = useState(false);

  const load = useCallback(async () => {
    if (!currentUser.google_calendar_connected) { setLoading(false); return; }
    setLoading(true);
    try {
      const data = await api.getSuggestedTasks();
      setSuggestions(data.suggestions || []);
    } catch (err) {
      setSuggestions([]);
    } finally {
      setLoading(false);
    }
  }, [currentUser.google_calendar_connected]);

  useEffect(() => { load(); }, [load]);

  if (!currentUser.google_calendar_connected) return null;
  if (!loading && suggestions.length === 0) return null;

  function toggle(id) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  // Removes just this one occurrence, using its own specific id, so a different day's
  // occurrence of the same recurring event is completely unaffected and still shows up
  async function handleDismissOccurrence(suggestion) {
    setSuggestions((prev) => prev.filter((s) => s.id !== suggestion.id));
    setSelectedIds((prev) => {
      if (!prev.has(suggestion.id)) return prev;
      const next = new Set(prev);
      next.delete(suggestion.id);
      return next;
    });
    try {
      await api.dismissSuggestedTask(suggestion.id);
    } catch (err) {
      // if this fails, it simply reappears the next time suggestions are reloaded
    }
  }

  // The deliberate, permanent action: excludes every occurrence of the same recurring
  // series, past and future, using the shared series id rather than this one instance's id
  async function handleDismissForever(suggestion) {
    const seriesId = suggestion.series_id || suggestion.id;
    setSuggestions((prev) => prev.filter((s) => (s.series_id || s.id) !== seriesId));
    setSelectedIds((prev) => {
      const idsInSeries = suggestions.filter((s) => (s.series_id || s.id) === seriesId).map((s) => s.id);
      if (!idsInSeries.some((id) => prev.has(id))) return prev;
      const next = new Set(prev);
      idsInSeries.forEach((id) => next.delete(id));
      return next;
    });
    try {
      await api.dismissSuggestedTask(seriesId);
    } catch (err) {
      // if this fails, it simply reappears the next time suggestions are reloaded
    }
  }

  const selected = suggestions.filter((s) => selectedIds.has(s.id));

  return (
    <div className="cb-group">
      <div className="cb-group-head">
        <div className="cb-group-title">Suggested Tasks</div>
        <div className="cb-group-count">{suggestions.length}</div>
      </div>
      <div className="cb-hint" style={{ margin: "0 0 8px" }}>
        From your calendar, events without a meeting link. These are only suggestions, nothing is tracked, started, or completed until you add them.
      </div>
      <div className="cb-card-list">
        {loading && <div className="cb-empty">Checking your calendar...</div>}
        {!loading && suggestions.map((s) => (
          <label key={s.id} className="cb-checklist-item" style={{ cursor: "pointer", alignItems: "center" }}>
            <input type="checkbox" className="cb-checkbox" checked={selectedIds.has(s.id)} onChange={() => toggle(s.id)} />
            <div style={{ flex: 1 }}>
              <div className="cb-checklist-name">{s.summary}</div>
              <div className="cb-checklist-meta">
                {s.all_day ? "All day" : new Date(s.start).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })}
              </div>
            </div>
            <button
              className="cb-icon-btn" title="Remove just this one, it can still come back tomorrow if recurring"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); handleDismissOccurrence(s); }}
            >
              <Trash2 size={13} />
            </button>
            <button
              className="cb-icon-btn" title="Not needed, never suggest this again"
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); handleDismissForever(s); }}
            >
              <Ban size={13} />
            </button>
          </label>
        ))}
      </div>
      {selected.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <button className="cb-btn cb-btn-primary" onClick={() => setShowReview(true)}>
            Add {selected.length} Selected to To Do
          </button>
        </div>
      )}
      {showReview && (
        <SuggestedTasksReviewModal
          suggestions={selected} clients={clients} templates={templates} roles={roles} taskTypes={taskTypes} bankAccounts={bankAccounts}
          onClose={() => setShowReview(false)}
          onDone={async () => {
            setShowReview(false);
            setSelectedIds(new Set());
            await load();
          }}
          onCreateTasks={onCreateTasks}
        />
      )}
    </div>
  );
}

function Dashboard({ tasks, now, currentUser, members, isAdmin, onStart, onPause, onComplete, onDelete, onReassign, onReset, onNewTask, clients, templates, roles, taskTypes, bankAccounts, onCreateTasks }) {
  const [viewFilter, setViewFilter] = useState("everyone"); // "everyone" | "mine" | a member id
  const [showActiveOnly, setShowActiveOnly] = useState(false);
  const isSuperAdmin = currentUser.role === "super_admin";
  const pickableMembers = members.filter((m) => m.id !== currentUser.id && (isSuperAdmin || m.role !== "super_admin"));
  const viewedMember = pickableMembers.find((m) => m.id === viewFilter);

  const visibleTasks = (() => {
    if (!isAdmin || viewFilter === "everyone") return tasks;
    if (viewFilter === "mine") return tasks.filter((t) => t.owner_id === currentUser.id);
    return tasks.filter((t) => t.owner_id === viewFilter);
  })();

  const todo = visibleTasks.filter((t) => t.status === "todo");
  const inProgress = visibleTasks.filter((t) => t.status === "running" || t.status === "paused");
  const submittedToday = visibleTasks
    .filter((t) => t.status === "submitted" && isToday(t.submitted_at))
    .sort((a, b) => new Date(b.submitted_at) - new Date(a.submitted_at));

  const inProgressByClient = (() => {
    const map = new Map();
    for (const t of inProgress) {
      if (!map.has(t.client_name)) map.set(t.client_name, []);
      map.get(t.client_name).push(t);
    }
    return Array.from(map.keys())
      .sort((a, b) => a.localeCompare(b))
      .map((clientName) => ({
        clientName,
        items: map.get(clientName).sort((a, b) => (a.status === "running" ? -1 : b.status === "running" ? 1 : 0)),
      }));
  })();

  const myTasks = tasks.filter((t) => t.owner_id === currentUser.id);
  const todaySeconds = myTasks.reduce((sum, t) => {
    if (t.status === "submitted" && isToday(t.submitted_at)) return sum + elapsedSeconds(t, now);
    if (t.status === "running" || t.status === "paused") return sum + elapsedSeconds(t, now);
    return sum;
  }, 0);
  const myRunningCount = myTasks.filter((t) => t.status === "running" || t.status === "paused").length;
  const mySubmittedTodayCount = myTasks.filter((t) => t.status === "submitted" && isToday(t.submitted_at)).length;
  const activeNowCount = tasks.filter((t) => t.status === "running").length;

  if (showActiveOnly) {
    const runningTasks = tasks.filter((t) => t.status === "running");
    const byOwner = {};
    for (const t of runningTasks) {
      const owner = members.find((m) => m.id === t.owner_id);
      const label = owner ? owner.name : "Unknown";
      if (!byOwner[label]) byOwner[label] = [];
      byOwner[label].push(t);
    }
    const ownerNames = Object.keys(byOwner).sort();
    return (
      <div>
        <div className="cb-page-head">
          <div>
            <div className="cb-page-title cb-serif">Active right now</div>
            <div className="cb-page-sub">Everyone currently running a timer, across the whole firm.</div>
          </div>
          <button className="cb-btn" onClick={() => setShowActiveOnly(false)}>Back to dashboard</button>
        </div>
        <div className="cb-group">
          <div className="cb-card-list">
            {runningTasks.length === 0 ? (
              <div className="cb-empty">Nobody has a timer running right now.</div>
            ) : (
              ownerNames.map((name) => (
                <Fragment key={name}>
                  <div className="cb-client-subgroup-head"><Building2 size={12} />{name}</div>
                  {byOwner[name].map((t) => (
                    <TaskRow key={t.id} task={t} now={now} currentUser={currentUser} members={members}
                      onStart={onStart} onPause={onPause} onComplete={onComplete} onDelete={onDelete} onReassign={onReassign} onReset={onReset} />
                  ))}
                </Fragment>
              ))
            )}
          </div>
        </div>
      </div>
    );
  }

  function Group({ title, items, empty }) {
    return (
      <div className="cb-group">
        <div className="cb-group-head">
          <div className="cb-group-title">{title}</div>
          <div className="cb-group-count">{items.length}</div>
        </div>
        <div className="cb-card-list">
          {items.length === 0 ? (
            <div className="cb-empty">{empty}</div>
          ) : (
            items.map((t) => (
              <TaskRow key={t.id} task={t} now={now} currentUser={currentUser} members={members}
                onStart={onStart} onPause={onPause} onComplete={onComplete} onDelete={onDelete} onReassign={onReassign} onReset={onReset} />
            ))
          )}
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="cb-page-head">
        <div>
          <div className="cb-page-title cb-serif">Dashboard</div>
          <div className="cb-page-sub">
            {viewedMember
              ? `Just ${viewedMember.name.split(" ")[0]}'s active work.`
              : isAdmin && viewFilter === "mine"
                ? "Just your own active work."
                : "Your firm's active work, tracked client by client."}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {isAdmin && (
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div className="cb-tabs">
                <button className={`cb-tab ${viewFilter === "everyone" ? "active" : ""}`} onClick={() => setViewFilter("everyone")}>Everyone</button>
                <button className={`cb-tab ${viewFilter === "mine" ? "active" : ""}`} onClick={() => setViewFilter("mine")}>Just me</button>
              </div>
              {pickableMembers.length > 0 && (
                <select
                  className="cb-select"
                  style={{ width: 175, padding: "6px 24px 6px 10px", fontSize: 12.5 }}
                  value={viewedMember ? viewedMember.id : ""}
                  onChange={(e) => setViewFilter(e.target.value || "everyone")}
                >
                  <option value="">Or pick someone...</option>
                  {pickableMembers.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                </select>
              )}
            </div>
          )}
          <button className="cb-btn cb-btn-primary" onClick={onNewTask}><Plus size={15} />New task</button>
        </div>
      </div>

      <div className="cb-stats">
        <div className="cb-stat">
          <div className="cb-stat-num cb-mono">{formatHM(todaySeconds)}</div>
          <div className="cb-stat-label">Tracked today ({currentUser.name.split(" ")[0]})</div>
        </div>
        <div className="cb-stat">
          <div className="cb-stat-num">{myRunningCount}</div>
          <div className="cb-stat-label">Your tasks in progress</div>
        </div>
        <div className="cb-stat">
          <div className="cb-stat-num">{mySubmittedTodayCount}</div>
          <div className="cb-stat-label">Submitted today</div>
        </div>
        {isAdmin && (
          <div
            className="cb-stat" style={{ border: "2px solid var(--green)", cursor: "pointer" }}
            onClick={() => setShowActiveOnly(true)} title="See everyone currently running a timer"
          >
            <div className="cb-stat-num" style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <span className="cb-live-dot" />
              {activeNowCount}
            </div>
            <div className="cb-stat-label" style={{ color: "var(--green)", fontWeight: 500 }}>Active right now</div>
          </div>
        )}
      </div>

      <div className="cb-group">
        <div className="cb-group-head">
          <div className="cb-group-title">In progress</div>
          <div className="cb-group-count">{inProgress.length}</div>
        </div>
        <div className="cb-card-list">
          {inProgress.length === 0 ? (
            <div className="cb-empty">Nothing running or paused. Start a task below to begin tracking.</div>
          ) : (
            inProgressByClient.map((group) => (
              <Fragment key={group.clientName}>
                <div className="cb-client-subgroup-head"><Building2 size={12} />{group.clientName}</div>
                {group.items.map((t) => (
                  <TaskRow key={t.id} task={t} now={now} currentUser={currentUser} members={members}
                    onStart={onStart} onPause={onPause} onComplete={onComplete} onDelete={onDelete} onReassign={onReassign} onReset={onReset}
                    hideClient />
                ))}
              </Fragment>
            ))
          )}
        </div>
      </div>

      <Group title="To do" items={todo} empty={<span><span className="cb-empty-title">No tasks queued</span><br />Add one from a template or a one off task.</span>} />

      {(!isAdmin || viewFilter === "mine") && (
        <SuggestedTasksSection
          currentUser={currentUser} clients={clients} templates={templates} roles={roles} taskTypes={taskTypes} bankAccounts={bankAccounts}
          onCreateTasks={onCreateTasks}
        />
      )}

      <Group title="Submitted today" items={submittedToday} empty="Nothing submitted yet today." />
    </div>
  );
}

const PAY_PERIOD_LIMITS = { weekly: 52, fortnightly: 26, monthly: 12 };

function payPeriodLabel(type, number) {
  if (!type || !number) return "";
  if (type === "weekly") return `Week ${number}`;
  if (type === "fortnightly") return `Fortnight ${number}`;
  if (type === "monthly") return `Month ${number}`;
  return "";
}

function taskHeading(name, bankAccountName, payPeriodType, payPeriodNumber) {
  const parts = [name];
  if (bankAccountName) parts.push(bankAccountName);
  const periodLabel = payPeriodLabel(payPeriodType, payPeriodNumber);
  if (periodLabel) parts.push(periodLabel);
  return parts.join(" \u2014 ");
}

function SearchableSelect({ options, value, onChange, placeholder, getLabel, getSecondary }) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const containerRef = useRef(null);
  const selected = options.find((o) => o.id === value);

  useEffect(() => {
    function handleClickOutside(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const filtered = query.trim()
    ? options.filter((o) => getLabel(o).toLowerCase().includes(query.trim().toLowerCase()))
    : options;

  return (
    <div ref={containerRef} className="cb-searchable-wrap">
      <input
        className="cb-input"
        placeholder={placeholder}
        value={open ? query : (selected ? getLabel(selected) : "")}
        onFocus={() => { setOpen(true); setQuery(""); }}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
        onKeyDown={(e) => { if (e.key === "Escape") { e.target.blur(); setOpen(false); } }}
      />
      {open && (
        <div className="cb-searchable-list">
          {filtered.length === 0 && <div className="cb-searchable-empty">No matches</div>}
          {filtered.map((o) => (
            <div
              key={o.id}
              className="cb-searchable-item"
              onMouseDown={(e) => { e.preventDefault(); onChange(o.id); setOpen(false); setQuery(""); }}
            >
              {getLabel(o)}
              {getSecondary && getSecondary(o) && <span className="cb-searchable-secondary">{getSecondary(o)}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function NewTaskModal({ clients, templates, members, bankAccounts, roles, taskTypes, currentUser, onClose, onCreate, onAddClient }) {
  const [clientMode, setClientMode] = useState(clients.length ? "existing" : "new");
  const [clientId, setClientId] = useState(clients[0] ? clients[0].id : "");
  const [newClientName, setNewClientName] = useState("");

  const [taskMode, setTaskMode] = useState(templates.length ? "template" : "custom");
  const [templateId, setTemplateId] = useState("");
  const [selectedTaskIds, setSelectedTaskIds] = useState([]);
  const [bankAccountByTaskId, setBankAccountByTaskId] = useState({});
  const [roleByTaskId, setRoleByTaskId] = useState({});
  const [taskTypeByTaskId, setTaskTypeByTaskId] = useState({});
  const [payPeriodTypeByTaskId, setPayPeriodTypeByTaskId] = useState({});
  const [payPeriodNumberByTaskId, setPayPeriodNumberByTaskId] = useState({});
  const [taskSearchQuery, setTaskSearchQuery] = useState("");

  const [customName, setCustomName] = useState("");
  const [role, setRole] = useState("");
  const [taskType, setTaskType] = useState("");

  const [ownerId, setOwnerId] = useState(currentUser.id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const selectedTemplate = templates.find((t) => t.id === templateId) || null;
  const clientAccounts = clientMode === "existing" ? bankAccounts.filter((a) => a.client_id === clientId) : [];

  useEffect(() => {
    if (clientMode !== "new") return;
    setSelectedTaskIds((prev) =>
      prev.filter((id) => {
        const t = selectedTemplate && selectedTemplate.tasks.find((x) => x.id === id);
        return !(t && t.requires_bank_account);
      })
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientMode]);

  function pickTemplate(id) {
    setTemplateId(id);
    setSelectedTaskIds([]);
    setBankAccountByTaskId({});
    setRoleByTaskId({});
    setTaskTypeByTaskId({});
    setPayPeriodTypeByTaskId({});
    setPayPeriodNumberByTaskId({});
    setTaskSearchQuery("");
  }

  function toggleTaskId(id) {
    setSelectedTaskIds((prev) => {
      if (prev.includes(id)) {
        setBankAccountByTaskId((m) => { const next = { ...m }; delete next[id]; return next; });
        setRoleByTaskId((m) => { const next = { ...m }; delete next[id]; return next; });
        setTaskTypeByTaskId((m) => { const next = { ...m }; delete next[id]; return next; });
        setPayPeriodTypeByTaskId((m) => { const next = { ...m }; delete next[id]; return next; });
        setPayPeriodNumberByTaskId((m) => { const next = { ...m }; delete next[id]; return next; });
        return prev.filter((x) => x !== id);
      }
      // Pre-fill from the template task's own values, still changeable below
      const t = selectedTemplate && selectedTemplate.tasks.find((x) => x.id === id);
      setRoleByTaskId((m) => ({ ...m, [id]: t ? t.role || "" : "" }));
      setTaskTypeByTaskId((m) => ({ ...m, [id]: t ? t.task_type || "" : "" }));
      return [...prev, id];
    });
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    let cId = clientId, cName = "";
    try {
      setBusy(true);
      if (clientMode === "new") {
        if (!newClientName.trim()) { setBusy(false); return; }
        const c = await onAddClient(newClientName);
        cId = c.id; cName = c.name;
      } else {
        const c = clients.find((c) => c.id === clientId);
        if (!c) { setBusy(false); return; }
        cName = c.name;
      }

      let payloads = [];
      if (taskMode === "template") {
        if (!selectedTemplate || selectedTaskIds.length === 0) {
          setError("Select at least one task from the template");
          setBusy(false);
          return;
        }
        const chosenTasks = selectedTemplate.tasks.filter((t) => selectedTaskIds.includes(t.id));
        for (const t of chosenTasks) {
          if (t.requires_bank_account && !bankAccountByTaskId[t.id]) {
            setError(`Select a bank account for "${t.name}"`);
            setBusy(false);
            return;
          }
          if (!roleByTaskId[t.id]) {
            setError(`Select a role for "${t.name}"`);
            setBusy(false);
            return;
          }
          if (!taskTypeByTaskId[t.id]) {
            setError(`Select a task type for "${t.name}"`);
            setBusy(false);
            return;
          }
          if (t.needs_pay_period && (!payPeriodTypeByTaskId[t.id] || !payPeriodNumberByTaskId[t.id])) {
            setError(`Select a pay period for "${t.name}"`);
            setBusy(false);
            return;
          }
        }
        payloads = chosenTasks.map((t) => {
          const account = t.requires_bank_account ? bankAccounts.find((a) => a.id === bankAccountByTaskId[t.id]) : null;
          return {
            client_id: cId, client_name: cName, name: t.name,
            role: roleByTaskId[t.id], task_type: taskTypeByTaskId[t.id], owner_id: ownerId,
            bank_account_id: account ? account.id : null,
            bank_account_name: account ? account.name : "",
            tracks_number_label: t.tracks_number_label || "",
            pay_period_type: t.needs_pay_period ? payPeriodTypeByTaskId[t.id] : null,
            pay_period_number: t.needs_pay_period ? parseInt(payPeriodNumberByTaskId[t.id], 10) : null,
            source_template_name: selectedTemplate.name,
          };
        });
      } else {
        const name = customName.trim();
        if (!name) { setBusy(false); return; }
        if (!role) {
          setError("Select a role");
          setBusy(false);
          return;
        }
        if (!taskType) {
          setError("Select a task type");
          setBusy(false);
          return;
        }
        payloads = [{ client_id: cId, client_name: cName, name, role, task_type: taskType, owner_id: ownerId }];
      }

      await onCreate(payloads);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <div className="cb-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">New task</div>
          <button className="cb-icon-btn" onClick={onClose}><X size={16} /></button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="cb-modal-body">
            <div className="cb-field">
              <label className="cb-label">Client</label>
              {clients.length > 0 && (
                <div className="cb-tabs" style={{ marginBottom: 8, width: "fit-content" }}>
                  <button type="button" className={`cb-tab ${clientMode === "existing" ? "active" : ""}`} onClick={() => setClientMode("existing")}>Existing</button>
                  <button type="button" className={`cb-tab ${clientMode === "new" ? "active" : ""}`} onClick={() => setClientMode("new")}>New</button>
                </div>
              )}
              {clientMode === "existing" ? (
                <SearchableSelect
                  options={clients} value={clientId} onChange={setClientId}
                  placeholder="Search clients..." getLabel={(c) => c.name}
                />
              ) : (
                <input className="cb-input" placeholder="Client name" value={newClientName} onChange={(e) => setNewClientName(e.target.value)} autoFocus />
              )}
            </div>

            <div className="cb-field">
              <label className="cb-label">Task</label>
              {templates.length > 0 && (
                <div className="cb-tabs" style={{ marginBottom: 8, width: "fit-content" }}>
                  <button type="button" className={`cb-tab ${taskMode === "template" ? "active" : ""}`} onClick={() => setTaskMode("template")}>From template</button>
                  <button type="button" className={`cb-tab ${taskMode === "custom" ? "active" : ""}`} onClick={() => setTaskMode("custom")}>Custom</button>
                </div>
              )}

              {taskMode === "template" ? (
                <>
                  <div style={{ marginBottom: 10 }}>
                    <SearchableSelect
                      options={templates} value={templateId} onChange={pickTemplate}
                      placeholder="Search templates..." getLabel={(t) => t.name} getSecondary={(t) => t.field}
                    />
                  </div>
                  {selectedTemplate && (
                    <div className="cb-checklist">
                      {selectedTemplate.tasks.length === 0 && (
                        <div className="cb-hint" style={{ padding: 10 }}>This template has no tasks yet.</div>
                      )}
                      {selectedTemplate.tasks.length > 5 && (
                        <input
                          className="cb-input" placeholder="Search tasks in this template..."
                          value={taskSearchQuery} onChange={(e) => setTaskSearchQuery(e.target.value)}
                          style={{ margin: 8, width: "calc(100% - 16px)" }}
                        />
                      )}
                      {selectedTemplate.tasks.length > 0
                        && selectedTemplate.tasks.filter((t) => t.name.toLowerCase().includes(taskSearchQuery.toLowerCase())).length === 0 && (
                        <div className="cb-hint" style={{ padding: 10 }}>No tasks match "{taskSearchQuery}".</div>
                      )}
                      {selectedTemplate.tasks.filter((t) => t.name.toLowerCase().includes(taskSearchQuery.toLowerCase())).map((t) => {
                        const checked = selectedTaskIds.includes(t.id);
                        const disabledForNewClient = t.requires_bank_account && clientMode === "new";
                        return (
                          <div className="cb-checklist-item" key={t.id}>
                            <input
                              type="checkbox"
                              className="cb-checkbox"
                              checked={checked}
                              disabled={disabledForNewClient}
                              onChange={() => toggleTaskId(t.id)}
                            />
                            <div style={{ flex: 1 }}>
                              <div className="cb-checklist-name">{t.name}</div>
                              <div className="cb-checklist-meta">
                                {t.role || "no role"}{t.task_type ? ` \u00b7 ${t.task_type}` : ""}
                                {t.requires_bank_account ? " \u00b7 needs a bank account" : ""}
                                {t.needs_pay_period ? " \u00b7 needs a pay period" : ""}
                                {t.tracks_number_label ? ` \u00b7 tracks ${t.tracks_number_label.toLowerCase()}` : ""}
                              </div>
                              {checked && !disabledForNewClient && (
                                <div className="cb-field-row" style={{ marginTop: 6 }}>
                                  <select
                                    className="cb-select"
                                    value={roleByTaskId[t.id] || ""}
                                    onChange={(e) => setRoleByTaskId((prev) => ({ ...prev, [t.id]: e.target.value }))}
                                  >
                                    <option value="">Select a role</option>
                                    {roles.map((r) => <option key={r.id} value={r.name}>{r.name}</option>)}
                                  </select>
                                  <select
                                    className="cb-select"
                                    value={taskTypeByTaskId[t.id] || ""}
                                    onChange={(e) => setTaskTypeByTaskId((prev) => ({ ...prev, [t.id]: e.target.value }))}
                                  >
                                    <option value="">Select a task type</option>
                                    {taskTypes.map((tt) => <option key={tt.id} value={tt.name}>{tt.name}</option>)}
                                  </select>
                                </div>
                              )}
                              {checked && t.requires_bank_account && !disabledForNewClient && (
                                <select
                                  className="cb-select"
                                  style={{ marginTop: 6 }}
                                  value={bankAccountByTaskId[t.id] || ""}
                                  onChange={(e) => setBankAccountByTaskId((prev) => ({ ...prev, [t.id]: e.target.value }))}
                                >
                                  <option value="">Select a bank account</option>
                                  {clientAccounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                                </select>
                              )}
                              {checked && t.needs_pay_period && (
                                <div className="cb-field-row" style={{ marginTop: 6 }}>
                                  <select
                                    className="cb-select"
                                    value={payPeriodTypeByTaskId[t.id] || ""}
                                    onChange={(e) => {
                                      const value = e.target.value;
                                      setPayPeriodTypeByTaskId((prev) => ({ ...prev, [t.id]: value }));
                                      setPayPeriodNumberByTaskId((prev) => ({ ...prev, [t.id]: "" }));
                                    }}
                                  >
                                    <option value="">Weekly, fortnightly, or monthly?</option>
                                    <option value="weekly">Weekly</option>
                                    <option value="fortnightly">Fortnightly</option>
                                    <option value="monthly">Monthly</option>
                                  </select>
                                  {payPeriodTypeByTaskId[t.id] && (
                                    <select
                                      className="cb-select"
                                      value={payPeriodNumberByTaskId[t.id] || ""}
                                      onChange={(e) => setPayPeriodNumberByTaskId((prev) => ({ ...prev, [t.id]: e.target.value }))}
                                    >
                                      <option value="">Which period?</option>
                                      {Array.from({ length: PAY_PERIOD_LIMITS[payPeriodTypeByTaskId[t.id]] }, (_, i) => i + 1).map((n) => (
                                        <option key={n} value={n}>{payPeriodLabel(payPeriodTypeByTaskId[t.id], n)}</option>
                                      ))}
                                    </select>
                                  )}
                                </div>
                              )}
                              {checked && disabledForNewClient && (
                                <div className="cb-hint" style={{ marginTop: 4 }}>
                                  Add the client first, then add a bank account, then create this task.
                                </div>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </>
              ) : (
                <input className="cb-input" placeholder="e.g. Payroll review" value={customName} onChange={(e) => setCustomName(e.target.value)} />
              )}
            </div>

            {taskMode === "custom" && (
              <div className="cb-field-row">
                <div className="cb-field">
                  <label className="cb-label">Karbon role</label>
                  <select className="cb-select" value={role} onChange={(e) => setRole(e.target.value)}>
                    <option value="">Select a role</option>
                    {roles.map((r) => <option key={r.id} value={r.name}>{r.name}</option>)}
                  </select>
                </div>
                <div className="cb-field">
                  <label className="cb-label">Karbon task type</label>
                  <select className="cb-select" value={taskType} onChange={(e) => setTaskType(e.target.value)}>
                    <option value="">Select a task type</option>
                    {taskTypes.map((t) => <option key={t.id} value={t.name}>{t.name}</option>)}
                  </select>
                </div>
              </div>
            )}

            <div className="cb-field">
              <label className="cb-label">Assign to</label>
              {isAdminRole(currentUser.role) ? (
                <select className="cb-select" value={ownerId} onChange={(e) => setOwnerId(e.target.value)}>
                  {members.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
                </select>
              ) : (
                <div className="cb-input" style={{ background: "var(--paper)", color: "var(--ink-soft)" }}>{currentUser.name} (you)</div>
              )}
            </div>
            {error && <div className="cb-error">{error}</div>}
          </div>
          <div className="cb-modal-foot">
            <button type="button" className="cb-btn cb-btn-ghost" onClick={onClose}>Cancel</button>
            <button type="submit" className="cb-btn cb-btn-primary" disabled={busy}>
              {taskMode === "template" && selectedTaskIds.length > 1 ? `Add ${selectedTaskIds.length} tasks` : "Add to dashboard"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function StartCountModal({ task, onClose, onSubmit }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const label = task.tracks_number_label;
  return (
    <div className="cb-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">Before you start</div>
          <button className="cb-icon-btn" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="cb-modal-body">
          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 13, color: "var(--ink-soft)" }}>{task.client_name}{task.bank_account_name ? `: ${task.bank_account_name}` : ""}</div>
            <div style={{ fontSize: 16, fontWeight: 500 }}>{task.name}</div>
          </div>
          <div className="cb-field">
            <label className="cb-label">Starting {label.toLowerCase()}</label>
            <input
              type="number" className="cb-input" value={value} onChange={(e) => setValue(e.target.value)}
              autoFocus required
            />
          </div>
        </div>
        <div className="cb-modal-foot">
          <button className="cb-btn cb-btn-ghost" onClick={onClose}>Cancel</button>
          <button
            className="cb-btn cb-btn-primary" disabled={busy || value === ""}
            onClick={async () => { setBusy(true); await onSubmit(parseInt(value, 10)); }}
          >
            Start timer
          </button>
        </div>
      </div>
    </div>
  );
}

function CompleteModal({ task, now, roles, taskTypes, onClose, onSubmit }) {
  const [note, setNote] = useState(task.note || "");
  const [endCount, setEndCount] = useState(task.end_count != null ? String(task.end_count) : "");
  const [busy, setBusy] = useState(false);
  const [role, setRole] = useState(task.role || "");
  const [taskType, setTaskType] = useState(task.task_type || "");
  const total = elapsedSeconds(task, now);
  const trackedH = Math.floor(total / 3600);
  const trackedM = Math.round((total % 3600) / 60);
  const roundedTrackedSeconds = trackedH * 3600 + trackedM * 60;
  const [hours, setHours] = useState(String(trackedH));
  const [minutes, setMinutes] = useState(String(trackedM));
  const needsCount = !!task.tracks_number_label;
  const needsRole = !task.role;
  const needsTaskType = !task.task_type;

  const editedSeconds = (parseInt(hours || "0", 10) * 3600) + (parseInt(minutes || "0", 10) * 60);
  const isAdjusted = editedSeconds !== roundedTrackedSeconds;

  return (
    <div className="cb-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">Complete task</div>
          <button className="cb-icon-btn" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="cb-modal-body">
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 13, color: "var(--ink-soft)" }}>{task.client_name}{task.bank_account_name ? `: ${task.bank_account_name}` : ""}</div>
            <div style={{ fontSize: 16, fontWeight: 500 }}>{task.name}</div>
            <div style={{ fontSize: 12, color: "var(--ink-faint)", marginTop: 4 }}>
              {task.role || "no role set yet"}, {task.task_type || "no task type set yet"}
            </div>
          </div>
          {(needsRole || needsTaskType) && (
            <div className="cb-field-row">
              {needsRole && (
                <div className="cb-field">
                  <label className="cb-label">Role</label>
                  <select className="cb-select" value={role} onChange={(e) => setRole(e.target.value)}>
                    <option value="">Select a role</option>
                    {roles.map((r) => <option key={r.id} value={r.name}>{r.name}</option>)}
                  </select>
                </div>
              )}
              {needsTaskType && (
                <div className="cb-field">
                  <label className="cb-label">Task type</label>
                  <select className="cb-select" value={taskType} onChange={(e) => setTaskType(e.target.value)}>
                    <option value="">Select a task type</option>
                    {taskTypes.map((t) => <option key={t.id} value={t.name}>{t.name}</option>)}
                  </select>
                </div>
              )}
            </div>
          )}
          <div className="cb-field">
            <label className="cb-label">Time to record</label>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="number" min="0" className="cb-input" style={{ width: 80 }}
                value={hours} onChange={(e) => setHours(e.target.value)}
              />
              <span style={{ color: "var(--ink-soft)" }}>h</span>
              <input
                type="number" min="0" max="59" className="cb-input" style={{ width: 80 }}
                value={minutes} onChange={(e) => setMinutes(e.target.value)}
              />
              <span style={{ color: "var(--ink-soft)" }}>m</span>
            </div>
            <div className="cb-hint">
              Tracked: {formatHM(total)} ({decimalHours(total)}h){isAdjusted ? ", you are changing this" : ""}
            </div>
            {isAdjusted && (
              <div className="cb-hint">The tracked time is kept on record either way, it helps to explain the change in the note below.</div>
            )}
          </div>
          {needsCount && (
            <div className="cb-field">
              <label className="cb-label">Ending {task.tracks_number_label.toLowerCase()}</label>
              <input type="number" className="cb-input" value={endCount} onChange={(e) => setEndCount(e.target.value)} required />
              {task.start_count != null && (
                <div className="cb-hint">Started at {task.start_count}</div>
              )}
            </div>
          )}
          <div className="cb-field">
            <label className="cb-label"><StickyNote size={12} style={{ verticalAlign: -1, marginRight: 4 }} />Note (optional)</label>
            <textarea className="cb-textarea" placeholder="Anything worth flagging for this entry" value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
        </div>
        <div className="cb-modal-foot">
          <button className="cb-btn cb-btn-ghost" onClick={onClose}>Cancel</button>
          <button
            className="cb-btn cb-btn-primary" disabled={busy || (needsCount && endCount === "") || (needsRole && !role) || (needsTaskType && !taskType)}
            onClick={async () => {
              setBusy(true);
              await onSubmit(
                task.id, note, needsCount ? parseInt(endCount, 10) : null, isAdjusted ? editedSeconds : null,
                needsRole ? role : null, needsTaskType ? taskType : null
              );
            }}
          >
            <CheckCircle2 size={14} />Submit
          </button>
        </div>
      </div>
    </div>
  );
}

function AddMemberModal({ onClose, onAdd }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await onAdd(name, email, password);
      onClose();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <div className="cb-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">Add teammate</div>
          <button className="cb-icon-btn" onClick={onClose}><X size={16} /></button>
        </div>
        <form onSubmit={submit}>
          <div className="cb-modal-body">
            <div className="cb-field">
              <label className="cb-label">Name</label>
              <input className="cb-input" value={name} onChange={(e) => setName(e.target.value)} autoFocus placeholder="e.g. Priya Nair" required />
            </div>
            <div className="cb-field">
              <label className="cb-label">Email</label>
              <input className="cb-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="e.g. priya@firm.com" required />
            </div>
            <div className="cb-field">
              <label className="cb-label">Set a password for them</label>
              <input className="cb-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={8} required />
            </div>
            <div className="cb-hint">Share this password with them directly, there is no email sent automatically. They can log in with it right away.</div>
            {error && <div className="cb-error">{error}</div>}
          </div>
          <div className="cb-modal-foot">
            <button type="button" className="cb-btn cb-btn-ghost" onClick={onClose}>Cancel</button>
            <button type="submit" className="cb-btn cb-btn-primary" disabled={busy}>Add</button>
          </div>
        </form>
      </div>
    </div>
  );
}

function TemplateTaskEditor({ template, task, roles, taskTypes, trackedMetrics, onUpdateTask, onDeleteTask }) {
  const [name, setName] = useState(task.name);
  const [role, setRole] = useState(task.role);
  const [taskType, setTaskType] = useState(task.task_type);
  const [requiresBank, setRequiresBank] = useState(!!task.requires_bank_account);
  const [needsPayPeriod, setNeedsPayPeriod] = useState(!!task.needs_pay_period);
  const [tracksLabel, setTracksLabel] = useState(task.tracks_number_label);
  const [saving, setSaving] = useState(false);

  // Resyncs local state whenever the underlying task actually changes, e.g. after a save
  // completes and fresh data comes back, or if this same slot ends up showing a different
  // task. Editing always happens on a local copy, so two fields changed in quick succession
  // can never race each other and accidentally revert one of them.
  useEffect(() => {
    setName(task.name);
    setRole(task.role);
    setTaskType(task.task_type);
    setRequiresBank(!!task.requires_bank_account);
    setNeedsPayPeriod(!!task.needs_pay_period);
    setTracksLabel(task.tracks_number_label);
  }, [task.id, task.name, task.role, task.task_type, task.requires_bank_account, task.needs_pay_period, task.tracks_number_label]);

  const isDirty = name !== task.name || role !== task.role || taskType !== task.task_type
    || requiresBank !== !!task.requires_bank_account || needsPayPeriod !== !!task.needs_pay_period
    || tracksLabel !== task.tracks_number_label;

  async function handleSave() {
    setSaving(true);
    try {
      await onUpdateTask(template.id, task.id, {
        name, role, task_type: taskType, requires_bank_account: requiresBank,
        needs_pay_period: needsPayPeriod, tracks_number_label: tracksLabel,
      });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="cb-tmpl-task-block">
      <div className="cb-tmpl-task-row">
        <input className="cb-input" value={name} onChange={(e) => setName(e.target.value)} />
        <select className="cb-select" value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="">No role</option>
          {roles.map((r) => <option key={r.id} value={r.name}>{r.name}</option>)}
          {role && !roles.some((r) => r.name === role) && (
            <option value={role}>{role}</option>
          )}
        </select>
        <select className="cb-select" value={taskType} onChange={(e) => setTaskType(e.target.value)}>
          <option value="">No task type</option>
          {taskTypes.map((t) => <option key={t.id} value={t.name}>{t.name}</option>)}
          {taskType && !taskTypes.some((t) => t.name === taskType) && (
            <option value={taskType}>{taskType}</option>
          )}
        </select>
        <button className="cb-icon-btn cb-btn-danger" onClick={() => onDeleteTask(template.id, task.id)}><Trash2 size={13} /></button>
      </div>
      <div className="cb-tmpl-task-options">
        <label className="cb-tmpl-task-option-checkbox">
          <input
            type="checkbox" className="cb-checkbox" checked={requiresBank}
            onChange={(e) => setRequiresBank(e.target.checked)}
          />
          Requires a bank account
        </label>
        <label className="cb-tmpl-task-option-checkbox">
          <input
            type="checkbox" className="cb-checkbox" checked={needsPayPeriod}
            onChange={(e) => setNeedsPayPeriod(e.target.checked)}
          />
          Needs a pay period
        </label>
        <select
          className="cb-select cb-tmpl-task-tracks-input"
          value={tracksLabel}
          onChange={(e) => setTracksLabel(e.target.value)}
        >
          <option value="">Not tracked</option>
          {trackedMetrics.map((m) => <option key={m.id} value={m.name}>{m.name}</option>)}
          {tracksLabel && !trackedMetrics.some((m) => m.name === tracksLabel) && (
            <option value={tracksLabel}>{tracksLabel}</option>
          )}
        </select>
        {isDirty && (
          <button className="cb-btn cb-btn-sm cb-btn-primary" disabled={saving} onClick={handleSave} style={{ marginLeft: "auto" }}>
            {saving ? "Saving..." : "Save"}
          </button>
        )}
      </div>
    </div>
  );
}

function TemplateEditor({ template, isAdmin, roles, taskTypes, trackedMetrics, onAddTask, onUpdateTask, onDeleteTask, onDeleteTemplate, onRenameTemplate }) {
  const [isEditingHeader, setIsEditingHeader] = useState(false);
  const [editedField, setEditedField] = useState(template.field);
  const [editedName, setEditedName] = useState(template.name);
  const [renaming, setRenaming] = useState(false);

  function startEditingHeader(e) {
    e.stopPropagation();
    setEditedField(template.field);
    setEditedName(template.name);
    setIsEditingHeader(true);
  }

  async function saveHeader() {
    const field = editedField.trim();
    const name = editedName.trim();
    if (!field || !name) return;
    if (field === template.field && name === template.name) {
      setIsEditingHeader(false);
      return;
    }
    setRenaming(true);
    try {
      await onRenameTemplate(template.id, field, name);
      setIsEditingHeader(false);
    } finally {
      setRenaming(false);
    }
  }
  const [expanded, setExpanded] = useState(false);
  const [addingTask, setAddingTask] = useState(false);
  const [tName, setTName] = useState("");
  const [tRole, setTRole] = useState("");
  const [tType, setTType] = useState("");
  const [tRequiresBank, setTRequiresBank] = useState(false);
  const [tTracksLabel, setTTracksLabel] = useState("");

  async function addTask(e) {
    e.preventDefault();
    if (!tName.trim()) return;
    await onAddTask(template.id, {
      name: tName.trim(), role: tRole.trim(), task_type: tType.trim(),
      requires_bank_account: tRequiresBank, tracks_number_label: tTracksLabel.trim(),
    });
    setTName(""); setTRole(""); setTType(""); setTRequiresBank(false); setTTracksLabel(""); setAddingTask(false);
  }

  return (
    <div className="cb-tmpl-card">
      {isEditingHeader ? (
        <div className="cb-tmpl-head" style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <input className="cb-input" style={{ width: 160 }} value={editedField} onChange={(e) => setEditedField(e.target.value)} placeholder="Field" autoFocus />
          <input className="cb-input" style={{ width: 220 }} value={editedName} onChange={(e) => setEditedName(e.target.value)} placeholder="Template name" />
          <button className="cb-btn cb-btn-sm cb-btn-primary" disabled={renaming} onClick={saveHeader}>Save</button>
          <button className="cb-btn cb-btn-sm cb-btn-ghost" disabled={renaming} onClick={() => setIsEditingHeader(false)}>Cancel</button>
        </div>
      ) : (
        <button className="cb-tmpl-head cb-tmpl-head-toggle" onClick={() => setExpanded((v) => !v)}>
          <div>
            <div className="cb-tmpl-field" style={{ display: "flex", alignItems: "center", gap: 6 }}>
              {template.field}
              {isAdmin && (
                <span
                  role="button" tabIndex={0} title="Rename template" onClick={startEditingHeader}
                  onKeyDown={(e) => { if (e.key === "Enter") startEditingHeader(e); }}
                  className="cb-icon-btn" style={{ padding: 3 }}
                >
                  <Edit3 size={12} />
                </span>
              )}
            </div>
            <div className="cb-tmpl-name">{template.name}</div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 12, color: "var(--ink-soft)" }}>{template.tasks.length} task{template.tasks.length === 1 ? "" : "s"}</span>
            <ChevronDown size={16} style={{ transform: expanded ? "rotate(180deg)" : "none", transition: "transform 0.15s" }} />
          </div>
        </button>
      )}
      {expanded && (
        <>
          {isAdmin && (
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, padding: "10px 16px 0" }}>
              <button className="cb-btn cb-btn-sm" onClick={() => setAddingTask((v) => !v)}><Plus size={13} />Task</button>
              <button className="cb-icon-btn cb-btn-danger" title="Delete template" onClick={() => onDeleteTemplate(template.id)}><Trash2 size={14} /></button>
            </div>
          )}
          {template.tasks.map((t) =>
            isAdmin ? (
              <TemplateTaskEditor
                key={t.id} template={template} task={t} roles={roles} taskTypes={taskTypes} trackedMetrics={trackedMetrics}
                onUpdateTask={onUpdateTask} onDeleteTask={onDeleteTask}
              />
            ) : (
              <div className="cb-row" key={t.id}>
                <div className="cb-row-main">
                  <div className="cb-row-task">{t.name}</div>
                  <div className="cb-row-meta">
                    {t.role && <span>{t.role}</span>}
                    {t.task_type && <span>{t.task_type}</span>}
                    {t.requires_bank_account && <span>Needs a bank account</span>}
                    {t.tracks_number_label && <span>Tracks: {t.tracks_number_label}</span>}
                  </div>
                </div>
              </div>
            )
          )}
          {isAdmin && addingTask && (
            <form className="cb-tmpl-task-block" onSubmit={addTask}>
              <div className="cb-tmpl-task-row">
                <input className="cb-input" placeholder="New task name" value={tName} onChange={(e) => setTName(e.target.value)} autoFocus />
                <select className="cb-select" value={tRole} onChange={(e) => setTRole(e.target.value)}>
                  <option value="">No role</option>
                  {roles.map((r) => <option key={r.id} value={r.name}>{r.name}</option>)}
                </select>
                <select className="cb-select" value={tType} onChange={(e) => setTType(e.target.value)}>
                  <option value="">No task type</option>
                  {taskTypes.map((t) => <option key={t.id} value={t.name}>{t.name}</option>)}
                </select>
                <button type="submit" className="cb-icon-btn"><Plus size={13} /></button>
              </div>
              <div className="cb-tmpl-task-options">
                <label className="cb-tmpl-task-option-checkbox">
                  <input type="checkbox" className="cb-checkbox" checked={tRequiresBank} onChange={(e) => setTRequiresBank(e.target.checked)} />
                  Requires a bank account
                </label>
                <select
                  className="cb-select cb-tmpl-task-tracks-input"
                  value={tTracksLabel}
                  onChange={(e) => setTTracksLabel(e.target.value)}
                >
                  <option value="">Not tracked</option>
                  {trackedMetrics.map((m) => <option key={m.id} value={m.name}>{m.name}</option>)}
                </select>
              </div>
            </form>
          )}
          {template.tasks.length === 0 && !addingTask && <div className="cb-empty">No tasks yet in this template.</div>}
        </>
      )}
    </div>
  );
}

function SettingsView({
  roles, taskTypes, trackedMetrics, onAddRole, onDeleteRole, onAddTaskType, onDeleteTaskType,
  onAddTrackedMetric, onDeleteTrackedMetric, pods, isSuperAdmin, onAddPod, onDeletePod,
}) {
  const [newRole, setNewRole] = useState("");
  const [newPod, setNewPod] = useState("");
  const [newTaskType, setNewTaskType] = useState("");
  const [newMetric, setNewMetric] = useState("");
  const [error, setError] = useState("");

  async function submitRole(e) {
    e.preventDefault();
    if (!newRole.trim()) return;
    try {
      await onAddRole(newRole);
      setNewRole("");
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    }
  }

  async function submitTaskType(e) {
    e.preventDefault();
    if (!newTaskType.trim()) return;
    try {
      await onAddTaskType(newTaskType);
      setNewTaskType("");
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    }
  }

  async function submitMetric(e) {
    e.preventDefault();
    if (!newMetric.trim()) return;
    try {
      await onAddTrackedMetric(newMetric);
      setNewMetric("");
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    }
  }

  async function submitPod(e) {
    e.preventDefault();
    if (!newPod.trim()) return;
    try {
      await onAddPod(newPod);
      setNewPod("");
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    }
  }

  return (
    <div>
      <div className="cb-page-head">
        <div>
          <div className="cb-page-title cb-serif">Settings</div>
          <div className="cb-page-sub">The fixed lists everyone picks from when setting up templates or logging a task.</div>
        </div>
      </div>
      <div className="cb-tmpl-card">
        <div className="cb-tmpl-head">
          <div>
            <div className="cb-tmpl-field">Setup</div>
            <div className="cb-tmpl-name">Roles, task types, and tracked numbers</div>
          </div>
        </div>
        <div style={{ padding: 16, display: "flex", gap: 24, flexWrap: "wrap" }}>
          <div style={{ flex: 1, minWidth: 220 }}>
            <div className="cb-label" style={{ marginBottom: 8 }}>Roles</div>
            {roles.map((r) => (
              <div key={r.id} className="cb-client-account-row">
                <div style={{ fontSize: 13.5 }}>{r.name}</div>
                <button className="cb-icon-btn cb-btn-danger" onClick={() => onDeleteRole(r.id)}><Trash2 size={13} /></button>
              </div>
            ))}
            {roles.length === 0 && <div className="cb-hint" style={{ marginBottom: 8 }}>No roles added yet.</div>}
            <form onSubmit={submitRole} style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <input className="cb-input" placeholder="e.g. Manager" value={newRole} onChange={(e) => setNewRole(e.target.value)} />
              <button type="submit" className="cb-btn cb-btn-sm" style={{ flexShrink: 0 }}><Plus size={13} />Add</button>
            </form>
          </div>
          <div style={{ flex: 1, minWidth: 220 }}>
            <div className="cb-label" style={{ marginBottom: 8 }}>Task types</div>
            {taskTypes.map((t) => (
              <div key={t.id} className="cb-client-account-row">
                <div style={{ fontSize: 13.5 }}>{t.name}</div>
                <button className="cb-icon-btn cb-btn-danger" onClick={() => onDeleteTaskType(t.id)}><Trash2 size={13} /></button>
              </div>
            ))}
            {taskTypes.length === 0 && <div className="cb-hint" style={{ marginBottom: 8 }}>No task types added yet.</div>}
            <form onSubmit={submitTaskType} style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <input className="cb-input" placeholder="e.g. Advisory" value={newTaskType} onChange={(e) => setNewTaskType(e.target.value)} />
              <button type="submit" className="cb-btn cb-btn-sm" style={{ flexShrink: 0 }}><Plus size={13} />Add</button>
            </form>
          </div>
          <div style={{ flex: 1, minWidth: 220 }}>
            <div className="cb-label" style={{ marginBottom: 8 }}>Tracked numbers</div>
            {trackedMetrics.map((m) => (
              <div key={m.id} className="cb-client-account-row">
                <div style={{ fontSize: 13.5 }}>{m.name}</div>
                <button className="cb-icon-btn cb-btn-danger" onClick={() => onDeleteTrackedMetric(m.id)}><Trash2 size={13} /></button>
              </div>
            ))}
            {trackedMetrics.length === 0 && <div className="cb-hint" style={{ marginBottom: 8 }}>Nothing added yet.</div>}
            <form onSubmit={submitMetric} style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <input className="cb-input" placeholder="e.g. Unreconciled transactions" value={newMetric} onChange={(e) => setNewMetric(e.target.value)} />
              <button type="submit" className="cb-btn cb-btn-sm" style={{ flexShrink: 0 }}><Plus size={13} />Add</button>
            </form>
          </div>
          {error && <div className="cb-error" style={{ width: "100%" }}>{error}</div>}
        </div>
      </div>

      {isSuperAdmin && (
        <div className="cb-tmpl-card">
          <div className="cb-tmpl-head">
            <div>
              <div className="cb-tmpl-field">Teams</div>
              <div className="cb-tmpl-name">Pods</div>
            </div>
          </div>
          <div style={{ padding: 16 }}>
            <div className="cb-hint" style={{ marginBottom: 10 }}>
              Assign an admin to a pod on the Staff page and they will only see time and task data for people in that same pod. An admin with no pod keeps seeing everyone, and super admins always see everyone regardless of pod.
            </div>
            {pods.map((p) => (
              <div key={p.id} className="cb-client-account-row">
                <div style={{ fontSize: 13.5 }}>{p.name}</div>
                <button className="cb-icon-btn cb-btn-danger" onClick={() => onDeletePod(p.id)}><Trash2 size={13} /></button>
              </div>
            ))}
            {pods.length === 0 && <div className="cb-hint" style={{ marginBottom: 8 }}>No pods created yet.</div>}
            <form onSubmit={submitPod} style={{ display: "flex", gap: 8, marginTop: 8, maxWidth: 360 }}>
              <input className="cb-input" placeholder="e.g. Bookkeeping Pod 1" value={newPod} onChange={(e) => setNewPod(e.target.value)} />
              <button type="submit" className="cb-btn cb-btn-sm" style={{ flexShrink: 0 }}><Plus size={13} />Add</button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

function Templates({ templates, isAdmin, roles, taskTypes, trackedMetrics, onAddTask, onUpdateTask, onDeleteTask, onDeleteTemplate, onAddTemplate, onRenameTemplate }) {
  const [showNew, setShowNew] = useState(false);
  const [field, setField] = useState("");
  const [name, setName] = useState("");

  async function submitNew(e) {
    e.preventDefault();
    if (!field.trim() || !name.trim()) return;
    await onAddTemplate(field.trim(), name.trim());
    setField(""); setName(""); setShowNew(false);
  }

  return (
    <div>
      <div className="cb-page-head">
        <div>
          <div className="cb-page-title cb-serif">Templates</div>
          <div className="cb-page-sub">Standard task lists for each field of work, bookkeeping, payroll, tax, or anything else.</div>
        </div>
        {isAdmin && (
          <button className="cb-btn cb-btn-primary" onClick={() => setShowNew((v) => !v)}><Plus size={15} />New template</button>
        )}
      </div>

      {showNew && (
        <form className="cb-tmpl-card" onSubmit={submitNew} style={{ padding: 16 }}>
          <div className="cb-field-row">
            <div className="cb-field">
              <label className="cb-label">Field</label>
              <input className="cb-input" placeholder="e.g. Payroll" value={field} onChange={(e) => setField(e.target.value)} autoFocus />
            </div>
            <div className="cb-field">
              <label className="cb-label">Template name</label>
              <input className="cb-input" placeholder="e.g. Fortnightly payroll run" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button type="button" className="cb-btn cb-btn-ghost" onClick={() => setShowNew(false)}>Cancel</button>
            <button type="submit" className="cb-btn cb-btn-primary">Create</button>
          </div>
        </form>
      )}

      {templates.length === 0 ? (
        <div className="cb-empty"><span className="cb-empty-title">No templates yet</span><br />Create one to give the team a consistent starting list of tasks.</div>
      ) : (
        templates.map((t) => (
          <TemplateEditor
            key={t.id} template={t} isAdmin={isAdmin} roles={roles} taskTypes={taskTypes} trackedMetrics={trackedMetrics}
            onAddTask={onAddTask} onUpdateTask={onUpdateTask} onDeleteTask={onDeleteTask} onDeleteTemplate={onDeleteTemplate}
            onRenameTemplate={onRenameTemplate}
          />
        ))
      )}
    </div>
  );
}

function ClientRow({ client, taskCount, bankAccounts, isAdmin, allClients, onUpdateClient, onDeleteClient, onAddAccount, onDeleteAccount, onMergeClients }) {
  const [expanded, setExpanded] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [isEditingName, setIsEditingName] = useState(false);
  const [editedName, setEditedName] = useState(client.name);
  const [editedCode, setEditedCode] = useState(client.code || "");
  const [renaming, setRenaming] = useState(false);
  const [showMerge, setShowMerge] = useState(false);
  const [mergeTargetId, setMergeTargetId] = useState("");
  const [merging, setMerging] = useState(false);

  function startEditingName(e) {
    e.stopPropagation();
    setEditedName(client.name);
    setEditedCode(client.code || "");
    setIsEditingName(true);
  }

  async function saveClientName() {
    const trimmedName = editedName.trim();
    const trimmedCode = editedCode.trim();
    if (!trimmedName || (trimmedName === client.name && trimmedCode === (client.code || ""))) {
      setIsEditingName(false);
      return;
    }
    setRenaming(true);
    try {
      await onUpdateClient(client.id, trimmedName, trimmedCode);
      setIsEditingName(false);
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    } finally {
      setRenaming(false);
    }
  }

  async function handleMerge() {
    if (!mergeTargetId) return;
    const target = allClients.find((c) => c.id === mergeTargetId);
    if (!window.confirm(`Merge "${target.name}" into "${client.name}"? Every task and bank account from "${target.name}" will move to "${client.name}", and "${target.name}" will be deleted. This cannot be undone.`)) {
      return;
    }
    setMerging(true);
    try {
      await onMergeClients(client.id, mergeTargetId);
      setShowMerge(false);
      setMergeTargetId("");
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    } finally {
      setMerging(false);
    }
  }

  async function submitAccount(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      await onAddAccount(client.id, name);
      setName("");
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteClient() {
    try {
      await onDeleteClient(client.id);
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    }
  }

  async function handleDeleteAccount(id) {
    try {
      await onDeleteAccount(id);
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    }
  }

  return (
    <div>
      {isEditingName ? (
        <div className="cb-row" style={{ background: "var(--paper)" }}>
          <div className="cb-row-main" style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <input
              className="cb-input" style={{ maxWidth: 320 }} value={editedName}
              onChange={(e) => setEditedName(e.target.value)} autoFocus placeholder="Client name"
              onKeyDown={(e) => { if (e.key === "Enter") saveClientName(); if (e.key === "Escape") setIsEditingName(false); }}
            />
            <input
              className="cb-input" style={{ maxWidth: 140 }} value={editedCode}
              onChange={(e) => setEditedCode(e.target.value)} placeholder="Code (optional)"
              onKeyDown={(e) => { if (e.key === "Enter") saveClientName(); if (e.key === "Escape") setIsEditingName(false); }}
            />
            <button className="cb-btn cb-btn-sm cb-btn-primary" disabled={renaming} onClick={saveClientName}>Save</button>
            <button className="cb-btn cb-btn-sm cb-btn-ghost" disabled={renaming} onClick={() => setIsEditingName(false)}>Cancel</button>
            {error && <div className="cb-error" style={{ width: "100%" }}>{error}</div>}
          </div>
        </div>
      ) : (
        <button className="cb-client-row-toggle" onClick={() => setExpanded((v) => !v)}>
          <div className="cb-row-main">
            <div className="cb-row-task" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {client.name}
              {isAdmin && (
                <span
                  role="button" tabIndex={0} title="Edit client name" onClick={startEditingName}
                  onKeyDown={(e) => { if (e.key === "Enter") startEditingName(e); }}
                  className="cb-icon-btn" style={{ padding: 3 }}
                >
                  <Edit3 size={13} />
                </span>
              )}
            </div>
            <div className="cb-row-meta">
              {client.code && <span>Code: {client.code}</span>}
              {taskCount} task{taskCount === 1 ? "" : "s"} tracked, {bankAccounts.length} bank account{bankAccounts.length === 1 ? "" : "s"}
            </div>
          </div>
          <ChevronDown size={16} style={{ transform: expanded ? "rotate(180deg)" : "none", transition: "transform 0.15s" }} />
        </button>
      )}
      {expanded && (
        <div className="cb-client-detail">
          <div className="cb-hint" style={{ marginBottom: 8 }}>Bank accounts</div>
          {bankAccounts.length === 0 && <div className="cb-hint" style={{ marginBottom: 8 }}>No bank accounts added yet.</div>}
          {bankAccounts.map((a) => (
            <div key={a.id} className="cb-client-account-row">
              <div style={{ fontSize: 13.5 }}>{a.name}</div>
              {isAdmin && (
                <button className="cb-icon-btn cb-btn-danger" onClick={() => handleDeleteAccount(a.id)}><Trash2 size={13} /></button>
              )}
            </div>
          ))}
          <form onSubmit={submitAccount} style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <input className="cb-input" placeholder="e.g. ANZ Business Checking" value={name} onChange={(e) => setName(e.target.value)} />
            <button type="submit" className="cb-btn cb-btn-sm" disabled={busy} style={{ flexShrink: 0 }}><Plus size={13} />Add account</button>
          </form>
          {isAdmin && (
            <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--line)" }}>
              {!showMerge ? (
                <button className="cb-btn cb-btn-sm cb-btn-ghost" onClick={() => setShowMerge(true)}>Merge a duplicate into this client</button>
              ) : (
                <div>
                  <div className="cb-hint" style={{ marginBottom: 6 }}>
                    Pick the duplicate client to absorb. Every task and bank account from it moves here, and it gets deleted.
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                    <div style={{ width: 240 }}>
                      <SearchableSelect
                        options={allClients.filter((c) => c.id !== client.id)} value={mergeTargetId} onChange={setMergeTargetId}
                        placeholder="Search for the duplicate..." getLabel={(c) => c.name}
                      />
                    </div>
                    <button className="cb-btn cb-btn-sm cb-btn-danger" disabled={!mergeTargetId || merging} onClick={handleMerge}>
                      {merging ? "Merging..." : "Merge"}
                    </button>
                    <button className="cb-btn cb-btn-sm cb-btn-ghost" disabled={merging} onClick={() => { setShowMerge(false); setMergeTargetId(""); }}>Cancel</button>
                  </div>
                </div>
              )}
            </div>
          )}
          {isAdmin && (
            <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--line)" }}>
              <button
                className="cb-btn cb-btn-sm cb-btn-danger"
                title={taskCount > 0 ? "This client has tracked tasks" : "Delete client"}
                disabled={taskCount > 0}
                onClick={handleDeleteClient}
              >
                <Trash2 size={13} />Delete client
              </button>
            </div>
          )}
          {error && <div className="cb-error" style={{ marginTop: 8 }}>{error}</div>}
        </div>
      )}
    </div>
  );
}

function Clients({ clients, tasks, bankAccounts, isAdmin, onAdd, onUpdate, onDelete, onAddAccount, onDeleteAccount, onMergeClients }) {
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");

  async function submit(e) {
    e.preventDefault();
    if (!name.trim()) return;
    try {
      await onAdd(name, code);
      setName("");
      setCode("");
      setError("");
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 4000);
    }
  }

  return (
    <div>
      <div className="cb-page-head">
        <div>
          <div className="cb-page-title cb-serif">Clients</div>
          <div className="cb-page-sub">Every task on the dashboard is tracked against one of these. Click a client to manage its bank accounts.</div>
        </div>
      </div>
      <form onSubmit={submit} style={{ display: "flex", gap: 8, marginBottom: 6, maxWidth: 480 }}>
        <input className="cb-input" placeholder="New client name" value={name} onChange={(e) => setName(e.target.value)} />
        <input className="cb-input" style={{ maxWidth: 140 }} placeholder="Code (optional)" value={code} onChange={(e) => setCode(e.target.value)} />
        <button type="submit" className="cb-btn cb-btn-primary" style={{ flexShrink: 0 }}><Plus size={15} />Add</button>
      </form>
      {error && <div className="cb-error" style={{ marginBottom: 10 }}>{error}</div>}
      <div className="cb-card-list">
        {clients.length === 0 && <div className="cb-empty">No clients yet. Add your first one above.</div>}
        {clients.map((c) => {
          const count = tasks.filter((t) => t.client_id === c.id).length;
          const accounts = bankAccounts.filter((a) => a.client_id === c.id);
          return (
            <ClientRow
              key={c.id} client={c} taskCount={count} bankAccounts={accounts} isAdmin={isAdmin} allClients={clients}
              onUpdateClient={onUpdate} onDeleteClient={onDelete} onAddAccount={onAddAccount} onDeleteAccount={onDeleteAccount}
              onMergeClients={onMergeClients}
            />
          );
        })}
      </div>
    </div>
  );
}

function formatEventTime(iso, allDay) {
  if (allDay) return "All day";
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

function formatDayHeader(iso) {
  const d = new Date(iso);
  const today = new Date();
  const tomorrow = new Date(today);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(d, today)) return "Today";
  if (sameDay(d, tomorrow)) return "Tomorrow";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}

function CalendarPage({ onConnectCalendar }) {
  const [state, setState] = useState({ loading: true, connected: false, events: [], error: "" });

  const load = useCallback(async () => {
    setState((s) => ({ ...s, loading: true }));
    try {
      const data = await api.getCalendarEvents();
      setState({ loading: false, connected: data.connected, events: data.events || [], error: data.error || "" });
    } catch (err) {
      setState({ loading: false, connected: false, events: [], error: err.message || "Could not load your calendar" });
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const groups = [];
  for (const ev of state.events) {
    const dayKey = ev.start.slice(0, 10);
    let group = groups.find((g) => g.dayKey === dayKey);
    if (!group) { group = { dayKey, events: [] }; groups.push(group); }
    group.events.push(ev);
  }

  return (
    <div>
      <div className="cb-page-head">
        <div>
          <div className="cb-page-title cb-serif">Calendar</div>
          <div className="cb-page-sub">Your own upcoming events from Google Calendar, next 7 days.</div>
        </div>
        {state.connected && (
          <button className="cb-btn cb-btn-ghost" onClick={load} title="Re-check your calendar">
            <RotateCcw size={14} />Refresh
          </button>
        )}
      </div>

      {state.loading && <div className="cb-empty">Loading your calendar...</div>}

      {!state.loading && !state.connected && (
        <div className="cb-empty">
          <span className="cb-empty-title">Not connected yet</span><br />
          Connect your Google Calendar to see your upcoming events here, and to get a prompt to pause your timer automatically when a meeting starts.
          <div style={{ marginTop: 14 }}>
            <button className="cb-btn cb-btn-primary" onClick={onConnectCalendar}><CalendarIcon size={14} />Connect Google Calendar</button>
          </div>
        </div>
      )}

      {!state.loading && state.connected && state.error && (
        <div className="cb-error" style={{ marginBottom: 10 }}>{state.error}</div>
      )}

      {!state.loading && state.connected && !state.error && groups.length === 0 && (
        <div className="cb-empty">Nothing on your calendar for the next 7 days.</div>
      )}

      {!state.loading && state.connected && groups.map((g) => (
        <div className="cb-group" key={g.dayKey}>
          <div className="cb-group-head">
            <div className="cb-group-title">{formatDayHeader(g.events[0].start)}</div>
          </div>
          <div className="cb-card-list">
            {g.events.map((ev) => (
              <div className="cb-row" key={ev.id}>
                <div className="cb-row-main">
                  <div className="cb-row-task">{ev.summary}</div>
                  <div className="cb-row-meta">
                    <span>{formatEventTime(ev.start, ev.all_day)}</span>
                    {ev.has_meet_link && <span><Video size={11} style={{ verticalAlign: -2, marginRight: 3 }} />Google Meet</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ExportView({ members, clients, isAdmin, onTogglePushed, onDeleteTask }) {
  const [pushFilter, setPushFilter] = useState("pending");
  const [clientFilter, setClientFilter] = useState("all");
  const [staffFilter, setStaffFilter] = useState("all");
  const [datePreset, setDatePreset] = useState("this_week");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [copiedId, setCopiedId] = useState(null);
  const [rows, setRows] = useState([]);
  const [loadError, setLoadError] = useState("");
  const [expandedGroups, setExpandedGroups] = useState(() => new Set());

  const dateRange = useMemo(() => {
    if (datePreset === "custom") return dateRangeForCustom(customFrom, customTo);
    return dateRangeForPreset(datePreset);
  }, [datePreset, customFrom, customTo]);

  const loadRows = useCallback(async () => {
    try {
      const fromIso = dateRange ? dateRange.fromIso : null;
      const toIso = dateRange ? dateRange.toIso : null;
      const data = await api.getExportRows(clientFilter, pushFilter, fromIso, toIso, staffFilter);
      setRows(data);
      setLoadError("");
    } catch (err) {
      setLoadError(err.message || "Could not load the export");
    }
  }, [clientFilter, pushFilter, staffFilter, dateRange]);

  useEffect(() => { loadRows(); }, [loadRows]);

  const totalHours = rows.reduce((sum, r) => sum + r.hours, 0);

  // Groups by Client + Role + Task Type, since that combination is what actually becomes
  // one line on a Karbon timesheet, regardless of which template, or no template at all, the
  // underlying tasks came from. A group of exactly one task renders with no fold at all, the
  // fold only exists to earn its place when there is something to actually combine.
  const groups = useMemo(() => {
    const map = new Map();
    for (const r of rows) {
      const key = `${r.client}|||${r.role || ""}|||${r.task_type || ""}`;
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(r);
    }
    return Array.from(map.entries()).map(([key, groupRows]) => {
      const totalSeconds = groupRows.reduce((sum, r) => sum + r.seconds, 0);
      const allPushed = groupRows.every((r) => r.pushed);
      const anyAdjusted = groupRows.some((r) => r.adjusted);
      const dates = Array.from(new Set(groupRows.map((r) => (r.submitted_at || "").slice(0, 10))));
      const trackedBys = Array.from(new Set(groupRows.map((r) => r.tracked_by || "")));
      const bankAccounts = Array.from(new Set(groupRows.map((r) => r.bank_account || "")));
      const metrics = Array.from(new Set(groupRows.map((r) => r.metric || "")));
      const noteCount = groupRows.filter((r) => r.note).length;
      const label = (values) => (values.length === 1 ? (values[0] || "none") : "Multiple");
      return {
        key,
        client: groupRows[0].client,
        role: groupRows[0].role,
        task_type: groupRows[0].task_type,
        rows: groupRows,
        count: groupRows.length,
        totalSeconds,
        allPushed,
        anyAdjusted,
        noteCount,
        dateLabel: dates.length === 1 ? formatDate(groupRows[0].submitted_at) : `${dates.length} dates`,
        trackedByLabel: label(trackedBys),
        bankAccountLabel: label(bankAccounts),
        metricLabel: label(metrics),
      };
    });
  }, [rows]);

  function toggleGroup(key) {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  }

  function copyRow(r) {
    const decHours = r.hours.toFixed(2);
    const hm = formatHM(r.seconds);
    const countPart = r.change != null ? ` | ${r.metric}: ${r.start_count} to ${r.end_count} (${r.change > 0 ? "+" : ""}${r.change})` : "";
    const bankPart = r.bank_account ? ` | ${r.bank_account}` : "";
    const adjustedPart = r.adjusted ? ` | tracked ${formatHM(r.tracked_seconds)}, adjusted to ${hm}` : "";
    const text = `${r.client}${bankPart}: ${r.task} | Role: ${r.role || "none"} | Task type: ${r.task_type || "none"} | ${hm} (${decHours}h)${adjustedPart}${countPart}${r.note ? ` | Note: ${r.note}` : ""}`;
    copyToClipboard(text).then((ok) => {
      if (ok) { setCopiedId(r.id); setTimeout(() => setCopiedId(null), 1600); }
    });
  }

  function copyGroup(g) {
    const totalHm = formatHM(g.totalSeconds);
    const totalDec = (g.totalSeconds / 3600).toFixed(2);
    const breakdown = g.rows.map((r) => `${r.task}: ${formatHM(r.seconds)}`).join(", ");
    const text = `${g.client} | Role: ${g.role || "none"} | Task type: ${g.task_type || "none"} | ${totalHm} (${totalDec}h) total across ${g.count} tasks: ${breakdown}`;
    copyToClipboard(text).then((ok) => {
      if (ok) { setCopiedId(g.key); setTimeout(() => setCopiedId(null), 1600); }
    });
  }

  async function downloadCSV() {
    const fromIso = dateRange ? dateRange.fromIso : null;
    const toIso = dateRange ? dateRange.toIso : null;
    await downloadCsvFile(clientFilter, pushFilter, fromIso, toIso, `karbon-time-export-${new Date().toISOString().slice(0, 10)}.csv`, staffFilter);
  }
  async function copyCSV() {
    const fromIso = dateRange ? dateRange.fromIso : null;
    const toIso = dateRange ? dateRange.toIso : null;
    const text = await fetchCsvText(clientFilter, pushFilter, fromIso, toIso, staffFilter);
    copyToClipboard(text);
  }

  async function handleTogglePushed(id) {
    await onTogglePushed(id);
    await loadRows();
  }

  // Ticking Pushed on a folded group marks every task inside it at once, since that is
  // literally what happened, the combined total was entered as one Karbon line. Only the
  // tasks that actually need to change are touched, un-pushing a partially pushed group
  // brings everything to pushed first rather than guessing what was intended.
  async function handleToggleGroupPushed(g) {
    const target = !g.allPushed;
    for (const r of g.rows) {
      if (r.pushed !== target) {
        await onTogglePushed(r.id);
      }
    }
    await loadRows();
  }

  async function handleDelete(r) {
    if (window.confirm(`Delete this submitted entry for ${r.client}: ${r.task} (${formatHM(r.seconds)})? This cannot be undone.`)) {
      await onDeleteTask(r.id);
      await loadRows();
    }
  }

  return (
    <div>
      <div className="cb-page-head">
        <div>
          <div className="cb-page-title cb-serif">Export to Karbon</div>
          <div className="cb-page-sub">Karbon's public API cannot accept time entries yet, so bring this list into Karbon's timesheet manually. This keeps re-entry to seconds per line.</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="cb-btn" onClick={copyCSV}><Copy size={14} />Copy CSV</button>
          <button className="cb-btn cb-btn-primary" onClick={downloadCSV}><Download size={14} />Download CSV</button>
        </div>
      </div>

      <div className="cb-filter-bar">
        <div className="cb-tabs">
          <button className={`cb-tab ${datePreset === "this_week" ? "active" : ""}`} onClick={() => setDatePreset("this_week")}>This week</button>
          <button className={`cb-tab ${datePreset === "last_week" ? "active" : ""}`} onClick={() => setDatePreset("last_week")}>Last week</button>
          <button className={`cb-tab ${datePreset === "this_month" ? "active" : ""}`} onClick={() => setDatePreset("this_month")}>This month</button>
          <button className={`cb-tab ${datePreset === "last_month" ? "active" : ""}`} onClick={() => setDatePreset("last_month")}>Last month</button>
          <button className={`cb-tab ${datePreset === "custom" ? "active" : ""}`} onClick={() => setDatePreset("custom")}>Custom</button>
        </div>
        {datePreset === "custom" ? (
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input type="date" className="cb-input" style={{ width: 150 }} value={customFrom} onChange={(e) => setCustomFrom(e.target.value)} />
            <span style={{ color: "var(--ink-faint)" }}>to</span>
            <input type="date" className="cb-input" style={{ width: 150 }} value={customTo} onChange={(e) => setCustomTo(e.target.value)} />
          </div>
        ) : (
          dateRange && <div style={{ fontSize: 12.5, color: "var(--ink-soft)" }}>{dateRange.label}</div>
        )}
      </div>

      <div className="cb-filter-bar">
        <div className="cb-tabs">
          <button className={`cb-tab ${pushFilter === "pending" ? "active" : ""}`} onClick={() => setPushFilter("pending")}>Pending</button>
          <button className={`cb-tab ${pushFilter === "pushed" ? "active" : ""}`} onClick={() => setPushFilter("pushed")}>Pushed</button>
          <button className={`cb-tab ${pushFilter === "all" ? "active" : ""}`} onClick={() => setPushFilter("all")}>All</button>
        </div>
        <select className="cb-select" style={{ width: 200 }} value={clientFilter} onChange={(e) => setClientFilter(e.target.value)}>
          <option value="all">All clients</option>
          {clients.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        {isAdmin && (
          <select className="cb-select" style={{ width: 200 }} value={staffFilter} onChange={(e) => setStaffFilter(e.target.value)}>
            <option value="all">All staff</option>
            {members.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        )}
        <div style={{ marginLeft: "auto", fontSize: 13, color: "var(--ink-soft)" }}>
          {rows.length} entr{rows.length === 1 ? "y" : "ies"}, <span className="cb-mono" style={{ fontWeight: 600, color: "var(--ink)" }}>{totalHours.toFixed(2)}h</span> ({formatHM(totalHours * 3600)}) total
        </div>
      </div>

      {loadError && <div className="cb-error" style={{ marginBottom: 10 }}>{loadError}</div>}

      <div className="cb-table-wrap">
        <table className="cb-table">
          <thead>
            <tr>
              <th>Date</th><th>Client</th><th>Task</th><th>Role</th><th>Task type</th>
              <th className="num">Duration</th><th className="num">Tracked</th><th>Bank Account</th><th>Metric</th><th className="num">Change</th>
              <th>Note</th><th>Tracked by</th><th>Pushed</th><th></th>{isAdmin && <th></th>}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={isAdmin ? 15 : 14}><div className="cb-empty"><ClipboardList size={18} style={{ marginBottom: 6 }} /><br />Nothing here yet. Completed tasks show up once submitted.</div></td></tr>
            )}
            {groups.map((g) => {
              if (g.count === 1) {
                const r = g.rows[0];
                return (
                  <tr key={r.id}>
                    <td>{formatDate(r.submitted_at)}</td>
                    <td>{r.client}</td>
                    <td>{taskHeading(r.task, r.bank_account, r.pay_period_type, r.pay_period_number)}</td>
                    <td>{r.role || "none"}</td>
                    <td>{r.task_type || "none"}</td>
                    <td className="num cb-mono">{formatHM(r.seconds)}{r.adjusted && <span title="This time was edited at submission" style={{ color: "var(--amber)", marginLeft: 4 }}>*</span>}</td>
                    <td className="num cb-mono">{r.adjusted ? formatHM(r.tracked_seconds) : ""}</td>
                    <td>{r.bank_account || "none"}</td>
                    <td>{r.metric || "none"}</td>
                    <td className="num cb-mono">
                      {r.change != null ? `${r.start_count} \u2192 ${r.end_count} (${r.change > 0 ? "+" : ""}${r.change})` : "none"}
                    </td>
                    <td style={{ maxWidth: 200 }}>{r.note || "none"}</td>
                    <td>{r.tracked_by || "none"}</td>
                    <td><input type="checkbox" className="cb-checkbox" checked={r.pushed} onChange={() => handleTogglePushed(r.id)} /></td>
                    <td>
                      <button className="cb-icon-btn" title="Copy line" onClick={() => copyRow(r)}>
                        {copiedId === r.id ? <CheckCircle2 size={14} color="var(--green)" /> : <Copy size={14} />}
                      </button>
                    </td>
                    {isAdmin && (
                      <td>
                        <button className="cb-icon-btn cb-btn-danger" title="Delete" onClick={() => handleDelete(r)}>
                          <Trash2 size={14} />
                        </button>
                      </td>
                    )}
                  </tr>
                );
              }
              const expanded = expandedGroups.has(g.key);
              return (
                <Fragment key={g.key}>
                  <tr className="cb-export-group-row" style={{ cursor: "pointer", background: "var(--paper)" }} onClick={() => toggleGroup(g.key)}>
                    <td>{g.dateLabel}</td>
                    <td>{g.client}</td>
                    <td style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <ChevronDown size={14} style={{ transform: expanded ? "rotate(180deg)" : "none", transition: "transform 0.15s", flexShrink: 0 }} />
                      {g.count} tasks
                    </td>
                    <td>{g.role || "none"}</td>
                    <td>{g.task_type || "none"}</td>
                    <td className="num cb-mono" style={{ fontWeight: 600 }}>
                      {formatHM(g.totalSeconds)}
                      {g.anyAdjusted && <span title="At least one of these was edited at submission" style={{ color: "var(--amber)", marginLeft: 4 }}>*</span>}
                    </td>
                    <td></td>
                    <td>{g.bankAccountLabel}</td>
                    <td>{g.metricLabel}</td>
                    <td></td>
                    <td>{g.noteCount > 0 ? `${g.noteCount} note${g.noteCount > 1 ? "s" : ""}` : "none"}</td>
                    <td>{g.trackedByLabel}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" className="cb-checkbox" checked={g.allPushed} onChange={() => handleToggleGroupPushed(g)} />
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <button className="cb-icon-btn" title="Copy summary for Karbon" onClick={() => copyGroup(g)}>
                        {copiedId === g.key ? <CheckCircle2 size={14} color="var(--green)" /> : <Copy size={14} />}
                      </button>
                    </td>
                    {isAdmin && <td></td>}
                  </tr>
                  {expanded && g.rows.map((r) => (
                    <tr key={r.id} className="cb-export-group-child">
                      <td>{formatDate(r.submitted_at)}</td>
                      <td></td>
                      <td style={{ paddingLeft: 24, color: "var(--ink-soft)" }}>{taskHeading(r.task, r.bank_account, r.pay_period_type, r.pay_period_number)}</td>
                      <td>{r.role || "none"}</td>
                      <td>{r.task_type || "none"}</td>
                      <td className="num cb-mono">{formatHM(r.seconds)}{r.adjusted && <span title="This time was edited at submission" style={{ color: "var(--amber)", marginLeft: 4 }}>*</span>}</td>
                      <td className="num cb-mono">{r.adjusted ? formatHM(r.tracked_seconds) : ""}</td>
                      <td>{r.bank_account || "none"}</td>
                      <td>{r.metric || "none"}</td>
                      <td className="num cb-mono">
                        {r.change != null ? `${r.start_count} \u2192 ${r.end_count} (${r.change > 0 ? "+" : ""}${r.change})` : "none"}
                      </td>
                      <td style={{ maxWidth: 200 }}>{r.note || "none"}</td>
                      <td>{r.tracked_by || "none"}</td>
                      <td><input type="checkbox" className="cb-checkbox" checked={r.pushed} onChange={() => handleTogglePushed(r.id)} /></td>
                      <td>
                        <button className="cb-icon-btn" title="Copy line" onClick={() => copyRow(r)}>
                          {copiedId === r.id ? <CheckCircle2 size={14} color="var(--green)" /> : <Copy size={14} />}
                        </button>
                      </td>
                      {isAdmin && (
                        <td>
                          <button className="cb-icon-btn cb-btn-danger" title="Delete" onClick={() => handleDelete(r)}>
                            <Trash2 size={14} />
                          </button>
                        </td>
                      )}
                    </tr>
                  ))}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SetCredentialsModal({ memberName, initialEmail, isReset, onClose, onSubmit }) {
  const [email, setEmail] = useState(initialEmail || "");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await onSubmit(email, password);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <div className="cb-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">{isReset ? "Reset password for" : "Set up login for"} {memberName}</div>
          <button className="cb-icon-btn" onClick={onClose}><X size={16} /></button>
        </div>
        <form onSubmit={submit}>
          <div className="cb-modal-body">
            <div className="cb-field">
              <label className="cb-label">Email</label>
              <input className="cb-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required />
            </div>
            <div className="cb-field">
              <label className="cb-label">{isReset ? "New password" : "Password"}</label>
              <input className="cb-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={8} required />
            </div>
            <div className="cb-hint">Share this password with them directly.</div>
            {error && <div className="cb-error">{error}</div>}
          </div>
          <div className="cb-modal-foot">
            <button type="button" className="cb-btn cb-btn-ghost" onClick={onClose}>Cancel</button>
            <button type="submit" className="cb-btn cb-btn-primary" disabled={busy}>Save</button>
          </div>
        </form>
      </div>
    </div>
  );
}

function roleActions(m, currentUser) {
  const isSelf = m.id === currentUser.id;
  const canTouchSuperTier = isSelf || currentUser.role === "super_admin";
  const actions = [];
  if (m.role === "member") {
    actions.push({ label: "Make admin", target: "admin" });
  }
  if (m.role === "admin") {
    actions.push({ label: "Remove admin", target: "member" });
    if (canTouchSuperTier) {
      actions.push({ label: isSelf ? "Become super admin" : "Make super admin", target: "super_admin" });
    }
  }
  if (m.role === "super_admin" && canTouchSuperTier) {
    actions.push({ label: isSelf ? "Step down to admin" : "Remove super admin", target: "admin" });
  }
  return actions;
}

function roleLabel(role) {
  if (role === "super_admin") return "Super Admin";
  if (role === "admin") return "Admin";
  return "Member";
}

function StaffView({ members, currentUser, isAdmin, onAddMember, onChangeRole, onSetCredentials, onDeleteMember, onConnectCalendar, onDisconnectCalendar, pods, onAssignPod }) {
  const [settingUpId, setSettingUpId] = useState(null);
  const [error, setError] = useState("");
  const settingUpMember = members.find((m) => m.id === settingUpId);

  async function handleDelete(m) {
    if (!window.confirm(`Delete ${m.name}? This cannot be undone.`)) return;
    try {
      await onDeleteMember(m.id);
    } catch (err) {
      setError(err.message);
      setTimeout(() => setError(""), 5000);
    }
  }

  return (
    <div>
      <div className="cb-page-head">
        <div>
          <div className="cb-page-title cb-serif">Staff</div>
          <div className="cb-page-sub">Everyone with access to this workspace, and who has admin rights.</div>
        </div>
        {isAdmin && (
          <button className="cb-btn cb-btn-primary" onClick={onAddMember}><Plus size={15} />Add teammate</button>
        )}
      </div>
      {error && <div className="cb-error" style={{ marginBottom: 10 }}>{error}</div>}
      <div className="cb-card-list">
        {members.map((m) => (
          <div className="cb-row" key={m.id}>
            <div className="cb-row-main" style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <Avatar member={m} size={28} />
              <div>
                <div className="cb-row-task">{m.name}{m.id === currentUser.id ? " (you)" : ""}</div>
                <div className="cb-row-meta">
                  {roleLabel(m.role)}
                  {!m.email && " \u00b7 No login set up yet"}
                  {m.google_calendar_connected && " \u00b7 Calendar connected"}
                  {m.pod_id && ` \u00b7 ${(pods.find((p) => p.id === m.pod_id) || {}).name || "Unknown pod"}`}
                </div>
              </div>
            </div>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              {currentUser.role === "super_admin" && pods.length > 0 && (
                <select
                  className="cb-select"
                  style={{ width: 150, padding: "6px 24px 6px 10px", fontSize: 12.5 }}
                  value={m.pod_id || ""}
                  onChange={(e) => onAssignPod(m.id, e.target.value || null)}
                >
                  <option value="">No pod</option>
                  {pods.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              )}
              {m.id === currentUser.id && (
                m.google_calendar_connected ? (
                  <button className="cb-role-toggle" onClick={onDisconnectCalendar}>Disconnect calendar</button>
                ) : (
                  <button className="cb-role-toggle" onClick={onConnectCalendar}>Connect Google Calendar</button>
                )
              )}
              {isAdmin && (
                <button className="cb-role-toggle" onClick={() => setSettingUpId(m.id)}>
                  {m.email ? "Reset password" : "Set up login"}
                </button>
              )}
              {isAdmin && roleActions(m, currentUser).map((action) => (
                <button key={action.target} className="cb-role-toggle" onClick={() => onChangeRole(m.id, action.target)}>
                  {action.label}
                </button>
              ))}
              {isAdmin && m.id !== currentUser.id && (
                <button className="cb-icon-btn cb-btn-danger" title="Delete" onClick={() => handleDelete(m)}>
                  <Trash2 size={14} />
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
      {settingUpMember && (
        <SetCredentialsModal
          memberName={settingUpMember.name}
          initialEmail={settingUpMember.email}
          isReset={!!settingUpMember.email}
          onClose={() => setSettingUpId(null)}
          onSubmit={async (email, password) => {
            await onSetCredentials(settingUpMember.id, email, password);
            setSettingUpId(null);
          }}
        />
      )}
    </div>
  );
}

function niceDuration(ms) {
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function AlertsBanner({ onEnable, onDismiss }) {
  return (
    <div className="cb-notice">
      <span>Turn on alerts so Clockbook can reach you even if you are on a different tab or app when a timer gets paused.</span>
      <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
        <button className="cb-btn cb-btn-sm cb-btn-primary" onClick={onEnable}>Enable alerts</button>
        <button className="cb-btn cb-btn-sm cb-btn-ghost" onClick={onDismiss}>Not now</button>
      </div>
    </div>
  );
}

function SleepAlertModal({ alert, onDismiss, onResume }) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="cb-overlay">
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">Timer paused automatically</div>
        </div>
        <div className="cb-modal-body">
          <div style={{ lineHeight: 1.5 }}>
            This computer looks like it was locked or asleep for about{" "}
            <strong>{niceDuration(alert.gapMs)}</strong>. The timer for{" "}
            <strong>{alert.task.client_name}: {alert.task.name}</strong> was paused the moment it went away,
            so that time was not tracked.
          </div>
        </div>
        <div className="cb-modal-foot">
          <button className="cb-btn" disabled={busy} onClick={onDismiss}>Got it</button>
          <button className="cb-btn cb-btn-primary" disabled={busy} onClick={async () => { setBusy(true); await onResume(); }}>
            Resume timer
          </button>
        </div>
      </div>
    </div>
  );
}

function MeetingClientPickerModal({ meetingSummary, clients, onClose, onConfirm }) {
  const [clientId, setClientId] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <div className="cb-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">Track "{meetingSummary}"</div>
          <button className="cb-icon-btn" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="cb-modal-body">
          <div className="cb-field">
            <label className="cb-label">Which client is this for?</label>
            <SearchableSelect
              options={clients} value={clientId} onChange={setClientId}
              placeholder="Search clients..." getLabel={(c) => c.name}
            />
          </div>
          <div className="cb-hint">
            Role and task type can be filled in later, when you submit this. This starts tracking right away, with your other timer paused.
          </div>
        </div>
        <div className="cb-modal-foot">
          <button className="cb-btn cb-btn-ghost" onClick={onClose}>Cancel</button>
          <button
            className="cb-btn cb-btn-primary" disabled={!clientId || busy}
            onClick={async () => { setBusy(true); await onConfirm(clientId); }}
          >
            Start tracking
          </button>
        </div>
      </div>
    </div>
  );
}

function MeetingAlertModal({ alert, onDismiss, onPause, onTrackMeeting }) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="cb-overlay">
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">You have a meeting starting</div>
        </div>
        <div className="cb-modal-body">
          <div style={{ lineHeight: 1.5 }}>
            <strong>{alert.summary}</strong> looks like it's happening right now on your calendar.
            Want to pause the timer for <strong>{alert.task.client_name}: {alert.task.name}</strong> while you're in it,
            or track the meeting itself instead?
          </div>
        </div>
        <div className="cb-modal-foot" style={{ flexWrap: "wrap" }}>
          <button className="cb-btn" disabled={busy} onClick={onDismiss}>Keep it running</button>
          <button className="cb-btn" disabled={busy} onClick={async () => { setBusy(true); await onPause(); }}>
            Just pause
          </button>
          <button className="cb-btn cb-btn-primary" disabled={busy} onClick={onTrackMeeting}>
            Track this meeting instead
          </button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [authState, setAuthState] = useState("loading"); // loading | claim | login | ready
  const [unclaimedMembers, setUnclaimedMembers] = useState([]);
  const [currentUser, setCurrentUser] = useState(null);
  const [dataLoading, setDataLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [members, setMembers] = useState([]);
  const [clients, setClients] = useState([]);
  const [bankAccounts, setBankAccounts] = useState([]);
  const [roles, setRoles] = useState([]);
  const [taskTypes, setTaskTypes] = useState([]);
  const [trackedMetrics, setTrackedMetrics] = useState([]);
  const [pods, setPods] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [view, setView] = useState("dashboard");
  const [now, setNow] = useState(Date.now());
  const [toast, setToast] = useState(null);
  const [showNewTask, setShowNewTask] = useState(false);
  const [completingTask, setCompletingTask] = useState(null);
  const [startCountPrompt, setStartCountPrompt] = useState(null);
  const [showAddMember, setShowAddMember] = useState(false);
  const [sleepAlert, setSleepAlert] = useState(null);
  const [meetingAlert, setMeetingAlert] = useState(null);
  const [meetingTrackPrompt, setMeetingTrackPrompt] = useState(null);
  const [alertsBannerDismissed, setAlertsBannerDismissed] = useState(false);

  // Every timestamp actually saved comes from the server, so what gets recorded is always
  // accurate regardless of this computer's own clock. But the live ticking display for a
  // running task has to compare a server timestamp against this browser's own idea of "now",
  // and if the two clocks disagree, that shows up as a sudden jump the moment a timer
  // resumes. This measures the gap once and folds it into every tick so the live number
  // stays honest even when the computer's clock is wrong.
  const clockOffsetRef = useRef(0);

  useEffect(() => {
    (async () => {
      try {
        const before = Date.now();
        const { now: serverNow } = await api.getServerTime();
        const after = Date.now();
        const roundTripEstimate = (after - before) / 2;
        clockOffsetRef.current = new Date(serverNow).getTime() + roundTripEstimate - after;
      } catch (err) {
        // if this fails, the live display just falls back to trusting this computer's own clock
      }
    })();
  }, []);

  useEffect(() => {
    const iv = setInterval(() => setNow(Date.now() + clockOffsetRef.current), 1000);
    return () => clearInterval(iv);
  }, []);

  const showToast = useCallback((msg, isError) => {
    setToast({ msg, isError: !!isError });
    setTimeout(() => setToast(null), 3600);
  }, []);

  // Checks for a saved login on load. If it is missing or no longer valid, this finds out
  // whether the workspace already has passwords set up (show the login screen) or is being
  // upgraded from the old passwordless version (show the one time claim screen instead).
  useEffect(() => {
    (async () => {
      const token = getToken();
      if (token) {
        try {
          const me = await api.getMe();
          setCurrentUser(me);
          setAuthState("ready");
          return;
        } catch (err) {
          clearToken();
        }
      }
      try {
        const status = await api.getAuthStatus();
        if (status.setup_needed) {
          setUnclaimedMembers(status.unclaimed);
          setAuthState("claim");
        } else {
          setAuthState("login");
        }
      } catch (err) {
        setAuthState("login");
      }
    })();
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const [m, c, t, tk, b, r, tt, tm] = await Promise.all([
        api.getMembers(), api.getClients(), api.getTemplates(), api.getTasks(), api.getBankAccounts(),
        api.getRoles(), api.getTaskTypes(), api.getTrackedMetrics(),
      ]);
      setMembers(m);
      setClients(c);
      setTemplates(t);
      setTasks(tk);
      setBankAccounts(b);
      setRoles(r);
      setTaskTypes(tt);
      setTrackedMetrics(tm);
      setLoadError("");
      try {
        setPods(await api.getPods());
      } catch (err) {
        setPods([]); // staff cannot see this list at all, that is expected, not an error
      }
    } catch (err) {
      setLoadError(err.message || "Could not reach the server");
    }
  }, []);

  useEffect(() => {
    if (authState !== "ready") return;
    (async () => {
      await loadAll();
      setDataLoading(false);
    })();
  }, [authState, loadAll]);

  async function handleLogin(email, password) {
    const res = await api.login(email, password);
    setToken(res.token);
    setCurrentUser(res.member);
    setAuthState("ready");
  }

  async function handleClaim({ memberId, name, email, password }) {
    const res = await api.claimAccount({ member_id: memberId, name, email, password });
    setToken(res.token);
    setCurrentUser(res.member);
    setAuthState("ready");
  }

  async function handleLogout() {
    try {
      await api.logout();
    } catch (err) {
      // proceed with a local logout even if the server call fails
    }
    clearToken();
    setCurrentUser(null);
    setMembers([]);
    setClients([]);
    setTemplates([]);
    setTasks([]);
    setPods([]);
    setDataLoading(true);
    setAuthState("login");
  }

  // Keeps the dashboard in step with teammates working at the same time
  useEffect(() => {
    const iv = setInterval(async () => {
      try {
        const tk = await api.getTasks();
        setTasks(tk);
      } catch (err) {
        // a missed refresh is not worth interrupting the user, it will retry on the next tick
      }
    }, 8000);
    return () => clearInterval(iv);
  }, []);

  const myRunningTask = currentUser ? tasks.find((t) => t.owner_id === currentUser.id && t.status === "running") : null;

  function lastActivityTime(task) {
    if (!task.segments || task.segments.length === 0) return 0;
    const last = task.segments[task.segments.length - 1];
    return new Date(last.end || last.start).getTime();
  }

  const myMostRecentPaused = (() => {
    if (!currentUser) return null;
    const paused = tasks.filter((t) => t.owner_id === currentUser.id && t.status === "paused");
    if (paused.length === 0) return null;
    return paused.reduce((a, b) => (lastActivityTime(a) > lastActivityTime(b) ? a : b));
  })();

  // The header pins whichever task the person was last active on, running or paused, so
  // pausing does not make it disappear, it only gets replaced once another task starts.
  const myPinnedTask = myRunningTask || myMostRecentPaused;
  const isAdmin = currentUser ? isAdminRole(currentUser.role) : false;

  // Watches for this computer actually going to sleep or having its screen locked, and
  // pauses any running timer the moment it is detected rather than waiting to ask, since by
  // the time someone is back at their desk to answer a prompt the point of catching it
  // immediately is already lost. Deliberately does NOT react to switching tabs or apps,
  // since normal work involves moving between Clockbook, Excel, Xero, and email constantly,
  // and treating every one of those switches as "away" would pause people far too often.
  // Two signals are combined:
  //  1. A heartbeat gap: if far more time passed than expected between checks, the process
  //     itself was suspended, which happens during actual system sleep.
  //  2. The Idle Detection API: reports the OS screen lock state directly, but only on
  //     Chrome and Edge, and needs a one time permission grant, and only reports after the
  //     screen has been locked for at least a minute.
  const runningTaskRef = useRef(null);
  useEffect(() => { runningTaskRef.current = myRunningTask || null; }, [myRunningTask]);

  const lastAlertRef = useRef(0);
  const SLEEP_THRESHOLD_MS = 60000;

  // Pauses the given task right now, backdated to the moment it actually stopped running.
  // The backend already accepts an arbitrary historical end_at and uses it to close the
  // segment regardless of when this call itself arrives, so this is safe to call the instant
  // a lock is detected rather than waiting for the person to come back and unlock first.
  async function pauseTaskAt(task, atMs) {
    try {
      const updated = await api.pauseTask(task.id, new Date(atMs + clockOffsetRef.current).toISOString());
      mergeTask(updated);
    } catch (err) {
      // if this fails, the away alert below still tells the person to check the task themselves
    }
  }

  // Shows the same "you were away" popup and notification as before, kept separate from
  // pauseTaskAt so a screen lock can cut the timer off immediately while only bothering the
  // person with this once they are actually back to see it
  function showAwayAlert(task, gapMs, sleepStartMs) {
    setSleepAlert({ task, gapMs, sleepStartMs });
    // Firing this the instant the screen unlocks seems to land it in a window where Windows
    // delivers it straight to the notification center with no visible toast. Waiting a
    // couple of seconds is an attempt to land just outside that window instead, this is an
    // experiment, not a guaranteed fix, since it depends on Windows's own internal timing.
    setTimeout(() => {
      if ("Notification" in window && Notification.permission === "granted") {
        try {
          const n = new Notification("Clockbook", {
            body: `Timer for ${task.client_name}: ${task.name} was paused automatically after about ${niceDuration(gapMs)} away.`,
            tag: "clockbook-sleep-alert",
            requireInteraction: true,
          });
          n.onclick = () => window.focus();
        } catch (e) {
          // Some platforms restrict the Notification constructor, safe to ignore
        }
      }
    }, 2000);
  }

  async function reportGap(gapMs, sleepStartMs) {
    if (gapMs < SLEEP_THRESHOLD_MS) return;
    if (Date.now() - lastAlertRef.current < 5000) return; // avoid two detectors firing for the same gap
    lastAlertRef.current = Date.now();
    const task = runningTaskRef.current;
    if (!task) return;
    await pauseTaskAt(task, sleepStartMs);
    showAwayAlert(task, gapMs, sleepStartMs);
  }

  const wasHiddenSinceLastCheckRef = useRef(false);

  useEffect(() => {
    // This only ever records that the tab went hidden at some point, it never triggers
    // anything by itself, so switching tabs alone still cannot cause a pause on its own
    function handleVisibility() {
      if (document.hidden) wasHiddenSinceLastCheckRef.current = true;
    }
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  useEffect(() => {
    const HEARTBEAT_MS = 3000;
    let lastTick = Date.now();
    const iv = setInterval(() => {
      const nowTick = Date.now();
      const gap = nowTick - lastTick;
      const sleepStart = lastTick;
      lastTick = nowTick;
      // A tab that is not the visible one gets its timers deliberately throttled by the
      // browser to save power, and that throttling produces the exact same symptom as real
      // sleep, a bigger gap than expected between checks, with nothing actually having
      // happened. There is no length of time a hidden tab can be trusted to prove real
      // sleep, since ordinary work in another tab or app for several minutes produces
      // exactly this same signature, so a gap seen while hidden at any point is never
      // treated as sleep here, no matter how large. Genuine sleep is still caught whenever
      // this tab was the visible one throughout, and screen lock is covered separately by
      // the Idle Detection API below regardless of visibility.
      // Checking document.hidden only at the instant this callback happens to fire is not
      // good enough either, since a delayed callback often finally runs right after someone
      // has already switched back, by which point the tab looks visible again even though it
      // was hidden for the entire gap that caused the false reading. This instead remembers
      // whether the tab was hidden at any point since the last check.
      const wasHidden = wasHiddenSinceLastCheckRef.current || document.hidden;
      if (wasHidden) {
        if (!document.hidden) {
          // Back to visible now. This gap still spans the hidden period, so it is ignored
          // too, only the next one, entirely after this point, can be trusted.
          wasHiddenSinceLastCheckRef.current = false;
        }
        return;
      }
      reportGap(gap, sleepStart);
    }, HEARTBEAT_MS);
    return () => clearInterval(iv);
  }, []);

  const idleDetectorStartedRef = useRef(false);

  async function enableIdleDetection() {
    if (idleDetectorStartedRef.current) return;
    if (!("IdleDetector" in window)) return; // Not supported outside Chrome and Edge
    idleDetectorStartedRef.current = true;
    try {
      const permission = await window.IdleDetector.requestPermission();
      if (permission !== "granted") return;
      const controller = new AbortController();
      const detector = new window.IdleDetector();
      let lockedSince = null;
      let lockedTask = null;
      detector.addEventListener("change", () => {
        if (detector.screenState === "locked") {
          // Chrome fires this event again partway through the same lock once userState
          // separately shifts to idle, even though screenState is still just "locked". If a
          // lock is already being tracked, this later firing must be ignored, otherwise it
          // overwrites the correctly captured task with whatever is running now, which by
          // then is nothing, since the pause already happened on the first firing.
          if (lockedSince) return;
          // Pausing here, the moment the lock is detected, rather than waiting for the
          // person to come back and unlock, means the dashboard is accurate for anyone else
          // looking at it during a long lock, and nothing is lost if this computer never
          // comes back before someone eventually submits the task.
          lockedSince = Date.now();
          lockedTask = runningTaskRef.current;
          if (lockedTask && Date.now() - lastAlertRef.current >= 5000) {
            lastAlertRef.current = Date.now();
            pauseTaskAt(lockedTask, lockedSince);
          }
        } else if (detector.screenState === "unlocked" && lockedSince) {
          const gap = Date.now() - lockedSince;
          const sleepStart = lockedSince;
          const task = lockedTask;
          lockedSince = null;
          lockedTask = null;
          if (task) showAwayAlert(task, gap, sleepStart);
        }
      });
      // Chrome enforces a minimum threshold of 60000ms for this API
      await detector.start({ threshold: 60000, signal: controller.signal });
    } catch (err) {
      // Permission denied, unsupported context, or not triggered by a user gesture
    }
  }

  function handleEnableAlerts() {
    if ("Notification" in window && Notification.permission === "default") {
      Notification.requestPermission();
    }
    enableIdleDetection();
    setAlertsBannerDismissed(true);
  }

  // ---------------------------------------------------------------
  // Google Calendar meeting check (optional, per person, entirely separate from the sleep
  // and lock detection above). Only ever runs at all for someone who has connected their own
  // calendar, and only ever checks while they have a timer of their own running.
  // ---------------------------------------------------------------

  // Shows a one-time toast for the result of the Google OAuth redirect, then removes the
  // query param so refreshing the page does not show it again
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const calendarResult = params.get("calendar");
    if (!calendarResult) return;
    if (calendarResult === "connected") {
      showToast("Google Calendar connected");
      loadAll();
    } else if (calendarResult === "error") {
      showToast("Could not connect Google Calendar, please try again", true);
    }
    params.delete("calendar");
    const newSearch = params.toString();
    window.history.replaceState({}, "", window.location.pathname + (newSearch ? `?${newSearch}` : ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const promptedMeetingIdsRef = useRef(new Set());

  useEffect(() => {
    const iv = setInterval(async () => {
      if (!currentUser || !currentUser.google_calendar_connected) return;
      const task = runningTaskRef.current;
      if (!task) return;
      try {
        const { meeting } = await api.getMeetingNow();
        if (meeting && !promptedMeetingIdsRef.current.has(meeting.id)) {
          promptedMeetingIdsRef.current.add(meeting.id);
          setMeetingAlert({ task, summary: meeting.summary, meetingId: meeting.id });
          if ("Notification" in window && Notification.permission === "granted") {
            try {
              const n = new Notification("Clockbook", {
                body: `${meeting.summary} looks like it's starting now. Pause the timer for ${task.client_name}: ${task.name}?`,
                tag: "clockbook-meeting-alert",
                requireInteraction: true,
              });
              n.onclick = () => window.focus();
            } catch (e) {
              // Some platforms restrict the Notification constructor, safe to ignore
            }
          }
        }
      } catch (err) {
        // a missed check is not worth interrupting the person, it will retry on the next tick
      }
    }, 60000);
    return () => clearInterval(iv);
  }, [currentUser]);

  async function connectGoogleCalendar() {
    try {
      const { url } = await api.getGoogleConnectUrl();
      window.location.href = url;
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function disconnectGoogleCalendar() {
    try {
      const updated = await api.disconnectGoogleCalendar();
      setCurrentUser(updated);
      setMembers((prev) => prev.map((m) => (m.id === updated.id ? updated : m)));
      showToast("Google Calendar disconnected");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function addTeammate(name, email, password) {
    const member = await api.createMember(name.trim(), email.trim(), password);
    setMembers((prev) => [...prev, member]);
  }

  async function setMemberCredentials(memberId, email, password) {
    const updated = await api.setMemberCredentials(memberId, email.trim(), password);
    setMembers((prev) => prev.map((m) => (m.id === updated.id ? updated : m)));
  }

  async function deleteMember(memberId) {
    await api.deleteMember(memberId);
    setMembers((prev) => prev.filter((m) => m.id !== memberId));
  }

  async function changeMemberRole(memberId, role) {
    try {
      const updated = await api.updateMemberRole(memberId, role);
      setMembers((prev) => prev.map((m) => (m.id === updated.id ? updated : m)));
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function addPod(name) {
    try {
      const pod = await api.createPod(name.trim());
      setPods((prev) => [...prev, pod].sort((a, b) => a.name.localeCompare(b.name)));
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function deletePodHandler(id) {
    try {
      await api.deletePod(id);
      setPods((prev) => prev.filter((p) => p.id !== id));
      // members who were in this pod are now unassigned server-side, reflect that locally
      setMembers((prev) => prev.map((m) => (m.pod_id === id ? { ...m, pod_id: null } : m)));
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function assignMemberPod(memberId, podId) {
    try {
      const updated = await api.updateMemberPod(memberId, podId);
      setMembers((prev) => prev.map((m) => (m.id === updated.id ? updated : m)));
      if (updated.id === currentUser.id) setCurrentUser(updated);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function refreshTasks() {
    const tk = await api.getTasks();
    setTasks(tk);
  }

  function mergeTask(updated) {
    setTasks((prev) => prev.map((t) => (t.id === updated.id ? updated : t)));
  }

  async function startTask(taskId, startCount) {
    if (!currentUser) return;
    if ("Notification" in window && Notification.permission === "default") {
      // Tied to this click so the browser treats it as a genuine user request, not spam
      Notification.requestPermission();
    }
    enableIdleDetection();
    const previouslyRunning = tasks.find(
      (t) => t.owner_id === currentUser.id && t.status === "running" && t.id !== taskId
    );
    try {
      const updated = await api.startTask(taskId, startCount != null ? startCount : null);
      setTasks((prev) =>
        prev.map((t) => {
          if (t.id === updated.id) return updated;
          if (previouslyRunning && t.id === previouslyRunning.id) {
            const segs = t.segments.length ? [...t.segments] : [];
            if (segs.length && !segs[segs.length - 1].end) {
              segs[segs.length - 1] = { ...segs[segs.length - 1], end: new Date().toISOString() };
            }
            return { ...t, status: "paused", segments: segs };
          }
          return t;
        })
      );
      if (previouslyRunning) {
        showToast(`Paused "${previouslyRunning.client_name}: ${previouslyRunning.name}" to start this task`);
      }
    } catch (err) {
      showToast(err.message, true);
    }
  }

  // A task that tracks a number needs that starting figure once, the first time it is
  // started. Once start_count is set, resuming after a pause skips straight to starting.
  function requestStart(task) {
    if (task.tracks_number_label && task.start_count == null) {
      setStartCountPrompt(task);
    } else {
      startTask(task.id, null);
    }
  }

  async function pauseTask(taskId, endAt) {
    try {
      const updated = await api.pauseTask(taskId, endAt);
      mergeTask(updated);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function resetTask(taskId) {
    try {
      const updated = await api.resetTask(taskId);
      mergeTask(updated);
      showToast("Time reset");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function submitCompletion(taskId, note, endCount, adjustedSeconds, role, taskType) {
    try {
      const updated = await api.submitTask(taskId, note || "", endCount != null ? endCount : null, adjustedSeconds != null ? adjustedSeconds : null, role, taskType);
      mergeTask(updated);
      setCompletingTask(null);
      showToast("Task submitted");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function deleteTask(taskId) {
    try {
      await api.deleteTask(taskId);
      setTasks((prev) => prev.filter((t) => t.id !== taskId));
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function reassignTask(taskId, ownerId) {
    try {
      const updated = await api.reassignTask(taskId, ownerId);
      mergeTask(updated);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function togglePushed(taskId) {
    try {
      const updated = await api.togglePushed(taskId);
      mergeTask(updated);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function addClient(name, code) {
    const client = await api.createClient(name.trim(), code);
    setClients((prev) => [...prev, client]);
    return client;
  }

  async function updateClient(clientId, name, code) {
    const updated = await api.updateClient(clientId, name, code);
    setClients((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
    setTasks((prev) => prev.map((t) => (t.client_id === updated.id ? { ...t, client_name: updated.name } : t)));
  }

  async function mergeClients(keepId, duplicateId) {
    const keptClient = await api.mergeClients(keepId, duplicateId);
    setClients((prev) => prev.filter((c) => c.id !== duplicateId).map((c) => (c.id === keptClient.id ? keptClient : c)));
    setTasks((prev) => prev.map((t) => (t.client_id === duplicateId ? { ...t, client_id: keepId, client_name: keptClient.name } : t)));
    setBankAccounts((prev) => prev.map((a) => (a.client_id === duplicateId ? { ...a, client_id: keepId } : a)));
  }

  async function deleteClient(clientId) {
    await api.deleteClient(clientId);
    setClients((prev) => prev.filter((c) => c.id !== clientId));
  }

  async function addBankAccount(clientId, name) {
    const account = await api.createBankAccount(clientId, name.trim());
    setBankAccounts((prev) => [...prev, account]);
    return account;
  }

  async function deleteBankAccount(accountId) {
    await api.deleteBankAccount(accountId);
    setBankAccounts((prev) => prev.filter((a) => a.id !== accountId));
  }

  async function addRole(name) {
    const role = await api.createRole(name.trim());
    setRoles((prev) => [...prev, role]);
  }

  async function deleteRole(id) {
    await api.deleteRole(id);
    setRoles((prev) => prev.filter((r) => r.id !== id));
  }

  async function addTaskType(name) {
    const taskType = await api.createTaskType(name.trim());
    setTaskTypes((prev) => [...prev, taskType]);
  }

  async function deleteTaskType(id) {
    await api.deleteTaskType(id);
    setTaskTypes((prev) => prev.filter((t) => t.id !== id));
  }

  async function addTrackedMetric(name) {
    const metric = await api.createTrackedMetric(name.trim());
    setTrackedMetrics((prev) => [...prev, metric]);
  }

  async function deleteTrackedMetric(id) {
    await api.deleteTrackedMetric(id);
    setTrackedMetrics((prev) => prev.filter((m) => m.id !== id));
  }

  async function createTasks(payloads) {
    const created = [];
    for (const payload of payloads) {
      const task = await api.createTask(payload);
      created.push(task);
    }
    setTasks((prev) => [...created, ...prev]);
    setShowNewTask(false);
  }

  async function trackMeetingAsTask(clientId) {
    const { task: existingTask, summary, meetingId } = meetingTrackPrompt;
    const client = clients.find((c) => c.id === clientId);
    try {
      await pauseTask(existingTask.id);
      const created = await api.createTask({
        client_id: clientId, client_name: client.name, name: summary,
        source_calendar_event_id: meetingId || null,
      });
      setTasks((prev) => [created, ...prev]);
      await startTask(created.id, null);
      showToast(`Now tracking "${summary}"`);
    } catch (err) {
      showToast(err.message, true);
    } finally {
      setMeetingTrackPrompt(null);
    }
  }

  async function refreshTemplates() {
    const t = await api.getTemplates();
    setTemplates(t);
  }

  async function addTemplate(field, name) {
    await api.createTemplate(field, name);
    await refreshTemplates();
  }
  async function renameTemplate(id, field, name) {
    await api.updateTemplate(id, field, name);
    await refreshTemplates();
  }
  async function deleteTemplate(id) {
    await api.deleteTemplate(id);
    await refreshTemplates();
  }
  async function addTemplateTask(templateId, task) {
    await api.addTemplateTask(templateId, task);
    await refreshTemplates();
  }
  async function updateTemplateTask(templateId, taskId, task) {
    await api.updateTemplateTask(templateId, taskId, task);
    await refreshTemplates();
  }
  async function deleteTemplateTask(templateId, taskId) {
    await api.deleteTemplateTask(templateId, taskId);
    await refreshTemplates();
  }

  if (authState === "loading") {
    return <div className="cb-root"><LoadingScreen /></div>;
  }
  if (authState === "claim") {
    return <div className="cb-root"><ClaimScreen unclaimed={unclaimedMembers} onClaim={handleClaim} /></div>;
  }
  if (authState === "login") {
    return <div className="cb-root"><LoginScreen onLogin={handleLogin} /></div>;
  }
  if (dataLoading) {
    return <div className="cb-root"><LoadingScreen /></div>;
  }
  if (loadError) {
    return (
      <div className="cb-root">
        <div className="cb-center-screen">
          <div className="cb-welcome">
            <div className="cb-welcome-title cb-serif">Cannot reach the server</div>
            <div className="cb-welcome-sub">{loadError}</div>
            <button className="cb-btn cb-btn-primary" style={{ width: "100%", justifyContent: "center" }} onClick={loadAll}>Try again</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="cb-root">
      <div className="cb-shell">
        <Sidebar view={view} setView={setView} isAdmin={isAdmin} />
        <div className="cb-main">
          <TopBar
            currentUser={currentUser}
            onLogout={handleLogout}
            pinnedTask={myPinnedTask}
            now={now}
            onPause={() => myRunningTask && pauseTask(myRunningTask.id)}
            onResume={() => myPinnedTask && requestStart(myPinnedTask)}
            onComplete={() => myPinnedTask && setCompletingTask(myPinnedTask)}
          />
          {"Notification" in window && Notification.permission === "default" && !alertsBannerDismissed && (
            <AlertsBanner onEnable={handleEnableAlerts} onDismiss={() => setAlertsBannerDismissed(true)} />
          )}
          <div className="cb-content">
            {view === "dashboard" && (
              <Dashboard
                tasks={tasks} now={now} currentUser={currentUser} members={members} isAdmin={isAdmin}
                onStart={requestStart} onPause={pauseTask} onComplete={setCompletingTask}
                onDelete={deleteTask} onReassign={reassignTask} onReset={resetTask} onNewTask={() => setShowNewTask(true)}
                clients={clients} templates={templates} roles={roles} taskTypes={taskTypes} bankAccounts={bankAccounts}
                onCreateTasks={createTasks}
              />
            )}
            {view === "templates" && (
              <Templates
                templates={templates} isAdmin={isAdmin} roles={roles} taskTypes={taskTypes} trackedMetrics={trackedMetrics}
                onAddTask={addTemplateTask} onUpdateTask={updateTemplateTask} onDeleteTask={deleteTemplateTask}
                onDeleteTemplate={deleteTemplate} onAddTemplate={addTemplate} onRenameTemplate={renameTemplate}
              />
            )}
            {view === "clients" && (
              <Clients
                clients={clients} tasks={tasks} bankAccounts={bankAccounts} isAdmin={isAdmin}
                onAdd={addClient} onUpdate={updateClient} onDelete={deleteClient} onAddAccount={addBankAccount} onDeleteAccount={deleteBankAccount}
                onMergeClients={mergeClients}
              />
            )}
            {view === "calendar" && (
              <CalendarPage onConnectCalendar={connectGoogleCalendar} />
            )}
            {view === "export" && (
              <ExportView
                members={members} clients={clients} isAdmin={isAdmin}
                onTogglePushed={togglePushed} onDeleteTask={deleteTask}
              />
            )}
            {view === "staff" && (
              <StaffView
                members={members} currentUser={currentUser} isAdmin={isAdmin}
                onAddMember={() => setShowAddMember(true)} onChangeRole={changeMemberRole}
                onSetCredentials={setMemberCredentials} onDeleteMember={deleteMember}
                onConnectCalendar={connectGoogleCalendar} onDisconnectCalendar={disconnectGoogleCalendar}
                pods={pods} onAssignPod={assignMemberPod}
              />
            )}
            {view === "settings" && isAdmin && (
              <SettingsView
                roles={roles} taskTypes={taskTypes} trackedMetrics={trackedMetrics}
                onAddRole={addRole} onDeleteRole={deleteRole} onAddTaskType={addTaskType} onDeleteTaskType={deleteTaskType}
                onAddTrackedMetric={addTrackedMetric} onDeleteTrackedMetric={deleteTrackedMetric}
                pods={pods} isSuperAdmin={currentUser.role === "super_admin"} onAddPod={addPod} onDeletePod={deletePodHandler}
              />
            )}
          </div>
        </div>
      </div>

      {showNewTask && (
        <NewTaskModal
          clients={clients} templates={templates} members={members} bankAccounts={bankAccounts}
          roles={roles} taskTypes={taskTypes} currentUser={currentUser}
          onClose={() => setShowNewTask(false)} onCreate={createTasks} onAddClient={addClient}
        />
      )}
      {startCountPrompt && (
        <StartCountModal
          task={startCountPrompt}
          onClose={() => setStartCountPrompt(null)}
          onSubmit={async (count) => {
            await startTask(startCountPrompt.id, count);
            setStartCountPrompt(null);
          }}
        />
      )}
      {completingTask && (
        <CompleteModal task={completingTask} now={now} roles={roles} taskTypes={taskTypes} onClose={() => setCompletingTask(null)} onSubmit={submitCompletion} />
      )}
      {showAddMember && (
        <AddMemberModal onClose={() => setShowAddMember(false)} onAdd={addTeammate} />
      )}
      {sleepAlert && (
        <SleepAlertModal
          alert={sleepAlert}
          onDismiss={() => setSleepAlert(null)}
          onResume={async () => {
            requestStart(sleepAlert.task);
            setSleepAlert(null);
          }}
        />
      )}
      {meetingAlert && (
        <MeetingAlertModal
          alert={meetingAlert}
          onDismiss={() => setMeetingAlert(null)}
          onPause={async () => {
            await pauseTask(meetingAlert.task.id);
            setMeetingAlert(null);
          }}
          onTrackMeeting={() => {
            setMeetingTrackPrompt(meetingAlert);
            setMeetingAlert(null);
          }}
        />
      )}
      {meetingTrackPrompt && (
        <MeetingClientPickerModal
          meetingSummary={meetingTrackPrompt.summary} clients={clients}
          onClose={() => setMeetingTrackPrompt(null)}
          onConfirm={trackMeetingAsTask}
        />
      )}
      {toast && <div className={`cb-toast ${toast.isError ? "error" : ""}`}>{toast.msg}</div>}
    </div>
  );
}
