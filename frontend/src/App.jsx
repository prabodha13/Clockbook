import { useState, useEffect, useMemo, useCallback, useRef } from "react";
import {
  Clock, Play, Pause, Plus, X, Trash2, Download, Copy,
  ChevronDown, Building2, LayoutDashboard, ListTree, FileSpreadsheet,
  CheckCircle2, StickyNote, ClipboardList,
} from "lucide-react";
import { api, exportCsvUrl } from "./api.js";

const MEMBER_TINTS = ["#245C43", "#B5590F", "#5B6660", "#5C4A8C", "#8C2F3A", "#2E5C7A"];
const MEMBER_ID_KEY = "clockbook-member-id";

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
  const initials = member.name.trim().slice(0, 2).toUpperCase();
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

function FirstRun({ onCreate }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="cb-center-screen">
      <div className="cb-welcome">
        <div className="cb-welcome-mark"><Clock size={22} /></div>
        <div className="cb-welcome-title cb-serif">Welcome to Clockbook</div>
        <div className="cb-welcome-sub">Time tracking built around clients and tasks, with a clean handoff into Karbon. What is your name?</div>
        <form onSubmit={async (e) => { e.preventDefault(); if (name.trim() && !busy) { setBusy(true); await onCreate(name); } }}>
          <div className="cb-field" style={{ textAlign: "left" }}>
            <input className="cb-input" placeholder="e.g. Marcus Okonkwo" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          </div>
          <button type="submit" className="cb-btn cb-btn-primary" style={{ width: "100%", justifyContent: "center" }} disabled={!name.trim() || busy}>
            Get started
          </button>
        </form>
      </div>
    </div>
  );
}

function Sidebar({ view, setView }) {
  const items = [
    { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
    { id: "templates", label: "Templates", icon: ListTree },
    { id: "clients", label: "Clients", icon: Building2 },
    { id: "export", label: "Export", icon: FileSpreadsheet },
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

function TopBar({ currentUser, members, onSwitch, onAddMember, runningTask, now, onPause, onComplete }) {
  const [open, setOpen] = useState(false);
  const elapsed = runningTask ? elapsedSeconds(runningTask, now) : 0;
  return (
    <div className="cb-topbar">
      {runningTask ? (
        <div className="cb-tracking">
          <div className="cb-tracking-dot" />
          <div className="cb-tracking-text">
            <div className="cb-tracking-label">Now tracking</div>
            <div className="cb-tracking-name">{runningTask.client_name}: {runningTask.name}</div>
          </div>
          <div className="cb-tracking-time cb-mono">{formatHMS(elapsed)}</div>
          <div className="cb-tracking-actions">
            <button className="cb-btn cb-btn-sm" onClick={onPause}><Pause size={13} />Pause</button>
            <button className="cb-btn cb-btn-sm cb-btn-primary" onClick={onComplete}><CheckCircle2 size={13} />Complete</button>
          </div>
        </div>
      ) : (
        <div style={{ color: "var(--ink-faint)", fontSize: 13 }}>No timer running</div>
      )}
      <div className="cb-user-menu">
        <button className="cb-user-btn" onClick={() => setOpen((o) => !o)}>
          <Avatar member={currentUser} />
          {currentUser.name}
          <ChevronDown size={14} />
        </button>
        {open && (
          <div className="cb-user-dropdown" onMouseLeave={() => setOpen(false)}>
            {members.map((m) => (
              <button key={m.id} className="cb-user-dropdown-item" onClick={() => { onSwitch(m.id); setOpen(false); }}>
                <Avatar member={m} size={20} />
                {m.name}
                {m.id === currentUser.id && <CheckCircle2 size={13} style={{ marginLeft: "auto", color: "var(--green)" }} />}
              </button>
            ))}
            <div className="cb-user-dropdown-divider" />
            <button className="cb-user-dropdown-item" onClick={() => { onAddMember(); setOpen(false); }}>
              <Plus size={14} />
              Add teammate
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function TaskRow({ task, now, currentUser, members, onStart, onPause, onComplete, onDelete, onReassign }) {
  const isMine = task.owner_id === currentUser.id;
  const elapsed = elapsedSeconds(task, now);
  const owner = members.find((m) => m.id === task.owner_id);
  return (
    <div className="cb-row">
      <div className="cb-row-main">
        <div className="cb-row-client"><Building2 size={11} />{task.client_name}</div>
        <div className="cb-row-task">{task.name}</div>
        <div className="cb-row-meta">
          {task.role && <span>{task.role}</span>}
          {task.task_type && <span>{task.task_type}</span>}
          <span>
            Owner:{" "}
            <select
              className="cb-owner-select"
              value={task.owner_id || ""}
              onChange={(e) => onReassign(task.id, e.target.value)}
              disabled={task.status === "running"}
            >
              {members.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          </span>
        </div>
      </div>
      <StatusBadge status={task.status} />
      <div className="cb-row-time cb-mono">{elapsed > 0 ? formatHM(elapsed) : "0m"}</div>
      <div className="cb-row-actions">
        {task.status !== "running" && (
          <button className="cb-icon-btn" title={isMine ? "Start" : `Owned by ${owner ? owner.name : "someone else"}`} disabled={!isMine} onClick={() => onStart(task.id)}>
            <Play size={14} />
          </button>
        )}
        {task.status === "running" && (
          <button className="cb-icon-btn" title="Pause" disabled={!isMine} onClick={() => onPause(task.id)}>
            <Pause size={14} />
          </button>
        )}
        {(task.status === "running" || task.status === "paused") && (
          <button className="cb-btn cb-btn-sm cb-btn-primary" disabled={!isMine} onClick={() => onComplete(task)}>
            Complete
          </button>
        )}
        {task.status === "todo" && (
          <button className="cb-icon-btn cb-btn-danger" title="Delete" onClick={() => onDelete(task.id)}>
            <Trash2 size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

function Dashboard({ tasks, now, currentUser, members, onStart, onPause, onComplete, onDelete, onReassign, onNewTask }) {
  const todo = tasks.filter((t) => t.status === "todo");
  const inProgress = tasks
    .filter((t) => t.status === "running" || t.status === "paused")
    .sort((a, b) => (a.status === "running" ? -1 : 1));
  const submittedToday = tasks
    .filter((t) => t.status === "submitted" && isToday(t.submitted_at))
    .sort((a, b) => new Date(b.submitted_at) - new Date(a.submitted_at));

  const myTasks = tasks.filter((t) => t.owner_id === currentUser.id);
  const todaySeconds = myTasks.reduce((sum, t) => {
    if (t.status === "submitted" && isToday(t.submitted_at)) return sum + elapsedSeconds(t, now);
    if (t.status === "running" || t.status === "paused") return sum + elapsedSeconds(t, now);
    return sum;
  }, 0);
  const myRunningCount = myTasks.filter((t) => t.status === "running" || t.status === "paused").length;
  const mySubmittedTodayCount = myTasks.filter((t) => t.status === "submitted" && isToday(t.submitted_at)).length;

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
                onStart={onStart} onPause={onPause} onComplete={onComplete} onDelete={onDelete} onReassign={onReassign} />
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
          <div className="cb-page-sub">Your firm's active work, tracked client by client.</div>
        </div>
        <button className="cb-btn cb-btn-primary" onClick={onNewTask}><Plus size={15} />New task</button>
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
      </div>

      <Group title="In progress" items={inProgress} empty="Nothing running or paused. Start a task below to begin tracking." />
      <Group title="To do" items={todo} empty={<span><span className="cb-empty-title">No tasks queued</span><br />Add one from a template or a one off task.</span>} />
      <Group title="Submitted today" items={submittedToday} empty="Nothing submitted yet today." />
    </div>
  );
}

function NewTaskModal({ clients, templates, members, currentUser, onClose, onCreate, onAddClient }) {
  const allTemplateTasks = useMemo(() => {
    const list = [];
    for (const tpl of templates) {
      for (const t of tpl.tasks) list.push({ ...t, field: tpl.field });
    }
    return list;
  }, [templates]);

  const [clientMode, setClientMode] = useState(clients.length ? "existing" : "new");
  const [clientId, setClientId] = useState(clients[0] ? clients[0].id : "");
  const [newClientName, setNewClientName] = useState("");
  const [taskMode, setTaskMode] = useState(allTemplateTasks.length ? "template" : "custom");
  const [templateTaskKey, setTemplateTaskKey] = useState(allTemplateTasks[0] ? allTemplateTasks[0].name : "");
  const [customName, setCustomName] = useState("");
  const [role, setRole] = useState(allTemplateTasks[0] ? allTemplateTasks[0].role : "");
  const [taskType, setTaskType] = useState(allTemplateTasks[0] ? allTemplateTasks[0].task_type : "");
  const [ownerId, setOwnerId] = useState(currentUser.id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function pickTemplateTask(name) {
    setTemplateTaskKey(name);
    const found = allTemplateTasks.find((t) => t.name === name);
    if (found) { setRole(found.role); setTaskType(found.task_type); }
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
      const name = taskMode === "template" ? templateTaskKey : customName.trim();
      if (!name) { setBusy(false); return; }
      await onCreate({ client_id: cId, client_name: cName, name, role, task_type: taskType, owner_id: ownerId });
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
                <select className="cb-select" value={clientId} onChange={(e) => setClientId(e.target.value)}>
                  {clients.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              ) : (
                <input className="cb-input" placeholder="Client name" value={newClientName} onChange={(e) => setNewClientName(e.target.value)} autoFocus />
              )}
            </div>

            <div className="cb-field">
              <label className="cb-label">Task</label>
              {allTemplateTasks.length > 0 && (
                <div className="cb-tabs" style={{ marginBottom: 8, width: "fit-content" }}>
                  <button type="button" className={`cb-tab ${taskMode === "template" ? "active" : ""}`} onClick={() => setTaskMode("template")}>From template</button>
                  <button type="button" className={`cb-tab ${taskMode === "custom" ? "active" : ""}`} onClick={() => setTaskMode("custom")}>Custom</button>
                </div>
              )}
              {taskMode === "template" ? (
                <select className="cb-select" value={templateTaskKey} onChange={(e) => pickTemplateTask(e.target.value)}>
                  {allTemplateTasks.map((t) => (
                    <option key={t.field + t.name} value={t.name}>{t.field}: {t.name}</option>
                  ))}
                </select>
              ) : (
                <input className="cb-input" placeholder="e.g. Payroll review" value={customName} onChange={(e) => setCustomName(e.target.value)} />
              )}
            </div>

            <div className="cb-field-row">
              <div className="cb-field">
                <label className="cb-label">Karbon role</label>
                <input className="cb-input" placeholder="e.g. Bookkeeper" value={role} onChange={(e) => setRole(e.target.value)} />
              </div>
              <div className="cb-field">
                <label className="cb-label">Karbon task type</label>
                <input className="cb-input" placeholder="e.g. Reconciliation" value={taskType} onChange={(e) => setTaskType(e.target.value)} />
              </div>
            </div>

            <div className="cb-field">
              <label className="cb-label">Assign to</label>
              <select className="cb-select" value={ownerId} onChange={(e) => setOwnerId(e.target.value)}>
                {members.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
            </div>
            {error && <div className="cb-error">{error}</div>}
          </div>
          <div className="cb-modal-foot">
            <button type="button" className="cb-btn cb-btn-ghost" onClick={onClose}>Cancel</button>
            <button type="submit" className="cb-btn cb-btn-primary" disabled={busy}>Add to dashboard</button>
          </div>
        </form>
      </div>
    </div>
  );
}

function CompleteModal({ task, now, onClose, onSubmit }) {
  const [note, setNote] = useState(task.note || "");
  const [busy, setBusy] = useState(false);
  const total = elapsedSeconds(task, now);
  return (
    <div className="cb-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">Complete task</div>
          <button className="cb-icon-btn" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="cb-modal-body">
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 13, color: "var(--ink-soft)" }}>{task.client_name}</div>
            <div style={{ fontSize: 16, fontWeight: 500 }}>{task.name}</div>
            <div className="cb-mono" style={{ fontSize: 24, fontWeight: 600, marginTop: 8, color: "var(--green)" }}>{formatHM(total)}</div>
            <div style={{ fontSize: 12, color: "var(--ink-faint)" }}>{decimalHours(total)} hours, {task.role || "no role set"}, {task.task_type || "no task type set"}</div>
          </div>
          <div className="cb-field">
            <label className="cb-label"><StickyNote size={12} style={{ verticalAlign: -1, marginRight: 4 }} />Note (optional)</label>
            <textarea className="cb-textarea" placeholder="Anything worth flagging for this entry" value={note} onChange={(e) => setNote(e.target.value)} />
          </div>
        </div>
        <div className="cb-modal-foot">
          <button className="cb-btn cb-btn-ghost" onClick={onClose}>Cancel</button>
          <button className="cb-btn cb-btn-primary" disabled={busy} onClick={async () => { setBusy(true); await onSubmit(task.id, note); }}>
            <CheckCircle2 size={14} />Submit
          </button>
        </div>
      </div>
    </div>
  );
}

function AddMemberModal({ onClose, onAdd }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="cb-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="cb-modal">
        <div className="cb-modal-head">
          <div className="cb-modal-title">Add teammate</div>
          <button className="cb-icon-btn" onClick={onClose}><X size={16} /></button>
        </div>
        <form onSubmit={async (e) => { e.preventDefault(); if (name.trim() && !busy) { setBusy(true); await onAdd(name); onClose(); } }}>
          <div className="cb-modal-body">
            <div className="cb-field">
              <label className="cb-label">Name</label>
              <input className="cb-input" value={name} onChange={(e) => setName(e.target.value)} autoFocus placeholder="e.g. Priya Nair" />
            </div>
            <div className="cb-hint">Anyone with access to this app can select this name. There is no password yet. A real login system is a natural next step once you are ready to open this up more widely.</div>
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

function TemplateEditor({ template, onAddTask, onUpdateTask, onDeleteTask, onDeleteTemplate }) {
  const [addingTask, setAddingTask] = useState(false);
  const [tName, setTName] = useState("");
  const [tRole, setTRole] = useState("");
  const [tType, setTType] = useState("");

  async function addTask(e) {
    e.preventDefault();
    if (!tName.trim()) return;
    await onAddTask(template.id, { name: tName.trim(), role: tRole.trim(), task_type: tType.trim() });
    setTName(""); setTRole(""); setTType(""); setAddingTask(false);
  }

  return (
    <div className="cb-tmpl-card">
      <div className="cb-tmpl-head">
        <div>
          <div className="cb-tmpl-field">{template.field}</div>
          <div className="cb-tmpl-name">{template.name}</div>
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button className="cb-btn cb-btn-sm" onClick={() => setAddingTask((v) => !v)}><Plus size={13} />Task</button>
          <button className="cb-icon-btn cb-btn-danger" title="Delete template" onClick={() => onDeleteTemplate(template.id)}><Trash2 size={14} /></button>
        </div>
      </div>
      {template.tasks.map((t) => (
        <div className="cb-tmpl-task-row" key={t.id}>
          <input className="cb-input" value={t.name} onChange={(e) => onUpdateTask(template.id, t.id, { name: e.target.value, role: t.role, task_type: t.task_type })} />
          <input className="cb-input" placeholder="Role" value={t.role} onChange={(e) => onUpdateTask(template.id, t.id, { name: t.name, role: e.target.value, task_type: t.task_type })} />
          <input className="cb-input" placeholder="Task type" value={t.task_type} onChange={(e) => onUpdateTask(template.id, t.id, { name: t.name, role: t.role, task_type: e.target.value })} />
          <button className="cb-icon-btn cb-btn-danger" onClick={() => onDeleteTask(template.id, t.id)}><Trash2 size={13} /></button>
        </div>
      ))}
      {addingTask && (
        <form className="cb-tmpl-task-row" onSubmit={addTask}>
          <input className="cb-input" placeholder="New task name" value={tName} onChange={(e) => setTName(e.target.value)} autoFocus />
          <input className="cb-input" placeholder="Role" value={tRole} onChange={(e) => setTRole(e.target.value)} />
          <input className="cb-input" placeholder="Task type" value={tType} onChange={(e) => setTType(e.target.value)} />
          <button type="submit" className="cb-icon-btn"><Plus size={13} /></button>
        </form>
      )}
      {template.tasks.length === 0 && !addingTask && <div className="cb-empty">No tasks yet in this template.</div>}
    </div>
  );
}

function Templates({ templates, onAddTask, onUpdateTask, onDeleteTask, onDeleteTemplate, onAddTemplate }) {
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
        <button className="cb-btn cb-btn-primary" onClick={() => setShowNew((v) => !v)}><Plus size={15} />New template</button>
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
            key={t.id} template={t}
            onAddTask={onAddTask} onUpdateTask={onUpdateTask} onDeleteTask={onDeleteTask} onDeleteTemplate={onDeleteTemplate}
          />
        ))
      )}
    </div>
  );
}

function Clients({ clients, tasks, onAdd, onDelete }) {
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  async function submit(e) {
    e.preventDefault();
    if (!name.trim()) return;
    await onAdd(name);
    setName("");
  }

  async function handleDelete(id) {
    try {
      await onDelete(id);
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
          <div className="cb-page-sub">Every task on the dashboard is tracked against one of these.</div>
        </div>
      </div>
      <form onSubmit={submit} style={{ display: "flex", gap: 8, marginBottom: 10, maxWidth: 420 }}>
        <input className="cb-input" placeholder="New client name" value={name} onChange={(e) => setName(e.target.value)} />
        <button type="submit" className="cb-btn cb-btn-primary" style={{ flexShrink: 0 }}><Plus size={15} />Add</button>
      </form>
      {error && <div className="cb-error" style={{ marginBottom: 10 }}>{error}</div>}
      <div className="cb-card-list">
        {clients.length === 0 && <div className="cb-empty">No clients yet. Add your first one above.</div>}
        {clients.map((c) => {
          const count = tasks.filter((t) => t.client_id === c.id).length;
          return (
            <div className="cb-row" key={c.id}>
              <div className="cb-row-main">
                <div className="cb-row-task">{c.name}</div>
                <div className="cb-row-meta">{count} task{count === 1 ? "" : "s"} tracked</div>
              </div>
              <button
                className="cb-icon-btn cb-btn-danger"
                title={count > 0 ? "This client has tracked tasks" : "Delete"}
                disabled={count > 0}
                onClick={() => handleDelete(c.id)}
              >
                <Trash2 size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ExportView({ tasks, members, clients, now, onTogglePushed }) {
  const [pushFilter, setPushFilter] = useState("pending");
  const [clientFilter, setClientFilter] = useState("all");
  const [copiedId, setCopiedId] = useState(null);

  const submitted = tasks.filter((t) => t.status === "submitted");

  const rows = useMemo(() => {
    return submitted
      .filter((t) => (clientFilter === "all" ? true : t.client_id === clientFilter))
      .filter((t) => {
        if (pushFilter === "all") return true;
        if (pushFilter === "pending") return !t.pushed_to_karbon;
        return !!t.pushed_to_karbon;
      })
      .sort((a, b) => new Date(b.submitted_at) - new Date(a.submitted_at))
      .map((t) => {
        const secs = elapsedSeconds(t, now);
        const trackedBy = (members.find((m) => m.id === t.submitted_by_id) || {}).name || "none";
        return {
          id: t.id,
          date: formatDate(t.submitted_at),
          client: t.client_name,
          task: t.name,
          role: t.role,
          taskType: t.task_type,
          decHours: decimalHours(secs),
          hm: formatHM(secs),
          note: t.note,
          trackedBy,
          pushed: !!t.pushed_to_karbon,
        };
      });
  }, [submitted, clientFilter, pushFilter, members, now]);

  const totalHours = rows.reduce((sum, r) => sum + parseFloat(r.decHours), 0);

  function copyRow(r) {
    const text = `${r.client}: ${r.task} | Role: ${r.role || "none"} | Task type: ${r.taskType || "none"} | ${r.hm} (${r.decHours}h)${r.note ? ` | Note: ${r.note}` : ""}`;
    copyToClipboard(text).then((ok) => {
      if (ok) { setCopiedId(r.id); setTimeout(() => setCopiedId(null), 1600); }
    });
  }

  function downloadCSV() {
    window.location.href = exportCsvUrl(clientFilter, pushFilter);
  }
  async function copyCSV() {
    const res = await fetch(exportCsvUrl(clientFilter, pushFilter));
    const text = await res.text();
    copyToClipboard(text);
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
          <button className={`cb-tab ${pushFilter === "pending" ? "active" : ""}`} onClick={() => setPushFilter("pending")}>Pending</button>
          <button className={`cb-tab ${pushFilter === "pushed" ? "active" : ""}`} onClick={() => setPushFilter("pushed")}>Pushed</button>
          <button className={`cb-tab ${pushFilter === "all" ? "active" : ""}`} onClick={() => setPushFilter("all")}>All</button>
        </div>
        <select className="cb-select" style={{ width: 200 }} value={clientFilter} onChange={(e) => setClientFilter(e.target.value)}>
          <option value="all">All clients</option>
          {clients.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <div style={{ marginLeft: "auto", fontSize: 13, color: "var(--ink-soft)" }}>
          {rows.length} entr{rows.length === 1 ? "y" : "ies"}, <span className="cb-mono" style={{ fontWeight: 600, color: "var(--ink)" }}>{totalHours.toFixed(2)}h</span> total
        </div>
      </div>

      <div className="cb-table-wrap">
        <table className="cb-table">
          <thead>
            <tr>
              <th>Date</th><th>Client</th><th>Task</th><th>Role</th><th>Task type</th>
              <th className="num">Duration</th><th>Note</th><th>Tracked by</th><th>Pushed</th><th></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={10}><div className="cb-empty"><ClipboardList size={18} style={{ marginBottom: 6 }} /><br />Nothing here yet. Completed tasks show up once submitted.</div></td></tr>
            )}
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.date}</td>
                <td>{r.client}</td>
                <td>{r.task}</td>
                <td>{r.role || "none"}</td>
                <td>{r.taskType || "none"}</td>
                <td className="num cb-mono">{r.hm}</td>
                <td style={{ maxWidth: 200 }}>{r.note || "none"}</td>
                <td>{r.trackedBy}</td>
                <td><input type="checkbox" className="cb-checkbox" checked={r.pushed} onChange={() => onTogglePushed(r.id)} /></td>
                <td>
                  <button className="cb-icon-btn" title="Copy line" onClick={() => copyRow(r)}>
                    {copiedId === r.id ? <CheckCircle2 size={14} color="var(--green)" /> : <Copy size={14} />}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function niceDuration(ms) {
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
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
            This computer looks like it was locked, asleep, or away from this tab for about{" "}
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

export default function App() {
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [members, setMembers] = useState([]);
  const [clients, setClients] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [currentUserId, setCurrentUserId] = useState(localStorage.getItem(MEMBER_ID_KEY) || null);
  const [view, setView] = useState("dashboard");
  const [now, setNow] = useState(Date.now());
  const [toast, setToast] = useState(null);
  const [showNewTask, setShowNewTask] = useState(false);
  const [completingTask, setCompletingTask] = useState(null);
  const [showAddMember, setShowAddMember] = useState(false);
  const [sleepAlert, setSleepAlert] = useState(null);

  useEffect(() => {
    const iv = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(iv);
  }, []);

  const showToast = useCallback((msg, isError) => {
    setToast({ msg, isError: !!isError });
    setTimeout(() => setToast(null), 3600);
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const [m, c, t, tk] = await Promise.all([
        api.getMembers(), api.getClients(), api.getTemplates(), api.getTasks(),
      ]);
      setMembers(m);
      setClients(c);
      setTemplates(t);
      setTasks(tk);
      setLoadError("");
    } catch (err) {
      setLoadError(err.message || "Could not reach the server");
    }
  }, []);

  useEffect(() => {
    (async () => {
      await loadAll();
      setLoading(false);
    })();
  }, [loadAll]);

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

  const currentUser = members.find((m) => m.id === currentUserId) || null;
  const myRunningTask = currentUser ? tasks.find((t) => t.owner_id === currentUser.id && t.status === "running") : null;

  // Watches for this computer or tab going away for a while, and pauses any running timer
  // the moment it is detected rather than waiting to ask, since by the time someone is back
  // at their desk to answer a prompt the point of catching it immediately is already lost.
  // Browsers give web pages no single reliable "the machine is locked" event, so three
  // signals are combined:
  //  1. A heartbeat gap: if far more time passed than expected between checks, the process
  //     itself was suspended, which happens during actual system sleep.
  //  2. Tab visibility: catches switching away or minimizing, though this does not reliably
  //     fire for an OS screen lock on every browser.
  //  3. The Idle Detection API: reports the OS screen lock state directly, but only on
  //     Chrome and Edge, and needs a one time permission grant, and only reports after the
  //     screen has been locked for at least a minute.
  const runningTaskRef = useRef(null);
  useEffect(() => { runningTaskRef.current = myRunningTask || null; }, [myRunningTask]);

  const lastAlertRef = useRef(0);
  const SLEEP_THRESHOLD_MS = 20000;

  async function reportGap(gapMs, sleepStartMs) {
    if (gapMs < SLEEP_THRESHOLD_MS) return;
    if (Date.now() - lastAlertRef.current < 5000) return; // avoid two detectors firing for the same gap
    lastAlertRef.current = Date.now();
    const task = runningTaskRef.current;
    if (!task) return;
    try {
      await api.pauseTask(task.id, new Date(sleepStartMs).toISOString());
      await refreshTasks();
    } catch (err) {
      // if this fails, still tell the person below so they know to check the task themselves
    }
    setSleepAlert({ task, gapMs, sleepStartMs });
    if ("Notification" in window && Notification.permission === "granted") {
      try {
        const n = new Notification("Clockbook", {
          body: `Timer for ${task.client_name}: ${task.name} was paused automatically after about ${niceDuration(gapMs)} away.`,
          tag: "clockbook-sleep-alert",
        });
        n.onclick = () => window.focus();
      } catch (e) {
        // Some platforms restrict the Notification constructor, safe to ignore
      }
    }
  }

  useEffect(() => {
    const HEARTBEAT_MS = 3000;
    let lastTick = Date.now();
    const iv = setInterval(() => {
      const nowTick = Date.now();
      const gap = nowTick - lastTick;
      const sleepStart = lastTick;
      lastTick = nowTick;
      reportGap(gap, sleepStart);
    }, HEARTBEAT_MS);
    return () => clearInterval(iv);
  }, []);

  useEffect(() => {
    let hiddenSince = null;
    function handleVisibility() {
      if (document.hidden) {
        hiddenSince = Date.now();
      } else if (hiddenSince) {
        const gap = Date.now() - hiddenSince;
        const sleepStart = hiddenSince;
        hiddenSince = null;
        reportGap(gap, sleepStart);
      }
    }
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
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
      detector.addEventListener("change", () => {
        if (detector.screenState === "locked") {
          lockedSince = Date.now();
        } else if (detector.screenState === "unlocked" && lockedSince) {
          const gap = Date.now() - lockedSince;
          const sleepStart = lockedSince;
          lockedSince = null;
          reportGap(gap, sleepStart);
        }
      });
      // Chrome enforces a minimum threshold of 60000ms for this API
      await detector.start({ threshold: 60000, signal: controller.signal });
    } catch (err) {
      // Permission denied, unsupported context, or not triggered by a user gesture
    }
  }

  async function createMember(name) {
    try {
      const member = await api.createMember(name.trim());
      setMembers((prev) => [...prev, member]);
      setCurrentUserId(member.id);
      localStorage.setItem(MEMBER_ID_KEY, member.id);
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function switchUser(id) {
    setCurrentUserId(id);
    localStorage.setItem(MEMBER_ID_KEY, id);
  }

  async function refreshTasks() {
    const tk = await api.getTasks();
    setTasks(tk);
  }

  async function startTask(taskId) {
    if (!currentUser) return;
    if ("Notification" in window && Notification.permission === "default") {
      // Tied to this click so the browser treats it as a genuine user request, not spam
      Notification.requestPermission();
    }
    enableIdleDetection();
    try {
      await api.startTask(taskId, currentUser.id);
      await refreshTasks();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function pauseTask(taskId, endAt) {
    try {
      await api.pauseTask(taskId, endAt);
      await refreshTasks();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function submitCompletion(taskId, note) {
    try {
      await api.submitTask(taskId, note || "");
      await refreshTasks();
      setCompletingTask(null);
      showToast("Task submitted");
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function deleteTask(taskId) {
    try {
      await api.deleteTask(taskId);
      await refreshTasks();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function reassignTask(taskId, ownerId) {
    try {
      await api.reassignTask(taskId, ownerId);
      await refreshTasks();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function togglePushed(taskId) {
    try {
      await api.togglePushed(taskId);
      await refreshTasks();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function addClient(name) {
    const client = await api.createClient(name.trim());
    setClients((prev) => [...prev, client]);
    return client;
  }

  async function deleteClient(clientId) {
    await api.deleteClient(clientId);
    setClients((prev) => prev.filter((c) => c.id !== clientId));
  }

  async function createTask(payload) {
    await api.createTask(payload);
    await refreshTasks();
    setShowNewTask(false);
  }

  async function refreshTemplates() {
    const t = await api.getTemplates();
    setTemplates(t);
  }

  async function addTemplate(field, name) {
    await api.createTemplate(field, name);
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

  if (loading) {
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
  if (!currentUser) {
    return <div className="cb-root"><FirstRun onCreate={createMember} /></div>;
  }

  return (
    <div className="cb-root">
      <div className="cb-shell">
        <Sidebar view={view} setView={setView} />
        <div className="cb-main">
          <TopBar
            currentUser={currentUser}
            members={members}
            onSwitch={switchUser}
            onAddMember={() => setShowAddMember(true)}
            runningTask={myRunningTask}
            now={now}
            onPause={() => myRunningTask && pauseTask(myRunningTask.id)}
            onComplete={() => myRunningTask && setCompletingTask(myRunningTask)}
          />
          <div className="cb-content">
            {view === "dashboard" && (
              <Dashboard
                tasks={tasks} now={now} currentUser={currentUser} members={members}
                onStart={startTask} onPause={pauseTask} onComplete={setCompletingTask}
                onDelete={deleteTask} onReassign={reassignTask} onNewTask={() => setShowNewTask(true)}
              />
            )}
            {view === "templates" && (
              <Templates
                templates={templates}
                onAddTask={addTemplateTask} onUpdateTask={updateTemplateTask} onDeleteTask={deleteTemplateTask}
                onDeleteTemplate={deleteTemplate} onAddTemplate={addTemplate}
              />
            )}
            {view === "clients" && (
              <Clients clients={clients} tasks={tasks} onAdd={addClient} onDelete={deleteClient} />
            )}
            {view === "export" && (
              <ExportView tasks={tasks} members={members} clients={clients} now={now} onTogglePushed={togglePushed} />
            )}
          </div>
        </div>
      </div>

      {showNewTask && (
        <NewTaskModal
          clients={clients} templates={templates} members={members} currentUser={currentUser}
          onClose={() => setShowNewTask(false)} onCreate={createTask} onAddClient={addClient}
        />
      )}
      {completingTask && (
        <CompleteModal task={completingTask} now={now} onClose={() => setCompletingTask(null)} onSubmit={submitCompletion} />
      )}
      {showAddMember && (
        <AddMemberModal onClose={() => setShowAddMember(false)} onAdd={createMember} />
      )}
      {sleepAlert && (
        <SleepAlertModal
          alert={sleepAlert}
          onDismiss={() => setSleepAlert(null)}
          onResume={async () => {
            await startTask(sleepAlert.task.id);
            setSleepAlert(null);
          }}
        />
      )}
      {toast && <div className={`cb-toast ${toast.isError ? "error" : ""}`}>{toast.msg}</div>}
    </div>
  );
}
