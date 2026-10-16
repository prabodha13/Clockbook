import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Lightbulb, ChevronDown, ChevronUp, X, Sun } from "lucide-react";
import { api } from "./api.js";
import "./dashboardEnhancements.css";

export function userLocalClock(now, zone) {
  const options = { timeZone: zone || Intl.DateTimeFormat().resolvedOptions().timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  try {
    const items = Object.fromEntries(new Intl.DateTimeFormat("en-GB", options).formatToParts(now).map(({ type, value }) => [type, value]));
    return { dayKey: `${items.year}-${items.month}-${items.day}`, year: +items.year, month: +items.month, day: +items.day, hour: +items.hour, date: new Intl.DateTimeFormat("en-GB", { timeZone: options.timeZone, weekday: "long", day: "numeric", month: "long", year: "numeric" }).format(now) };
  } catch {
    return userLocalClock(now, Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  }
}
export function greetingForHour(hour) { return hour >= 5 && hour < 12 ? "Good morning" : hour >= 12 && hour < 17 ? "Good afternoon" : "Good evening"; }
function hash(text) { let value = 2166136261; for (let i = 0; i < text.length; i++) value = Math.imul(value ^ text.charCodeAt(i), 16777619); return value >>> 0; }
function keyFor(user, day, suffix) { return `clockbook:${user.tenant_id || "tenant"}:${user.id}:${day}:${suffix}`; }

export function PerspectiveCard({ user, clock }) {
  const [perspectives, setPerspectives] = useState([]);
  const key = keyFor(user, clock.dayKey, "perspective-introduction");
  const [expanded, setExpanded] = useState(() => localStorage.getItem(key) !== "viewed");
  const [interacting, setInteracting] = useState(false);
  const [intro, setIntro] = useState(() => localStorage.getItem(key) !== "viewed");
  const card = useRef(null);
  useEffect(() => { let live = true; api.getPerspectives().then(v => { if (live) setPerspectives(v); }).catch(() => {}); return () => { live = false; }; }, [user.id, user.tenant_id]);
  useEffect(() => {
    const fresh = localStorage.getItem(key) !== "viewed";
    setIntro(fresh); setExpanded(fresh);
    if (fresh) localStorage.setItem(key, "viewed");
  }, [key]);
  useEffect(() => {
    if (!expanded || interacting) return;
    const timer = window.setTimeout(() => { setExpanded(false); setIntro(false); }, 15000);
    return () => window.clearTimeout(timer);
  }, [expanded, interacting, key]);
  const selected = useMemo(() => {
    if (!perspectives.length) return null;
    const pickKey = keyFor(user, clock.dayKey, "perspective-snapshot");
    try {
      const saved = JSON.parse(localStorage.getItem(pickKey) || "null");
      if (saved && typeof saved.text === "string" && saved.id) return saved;
    } catch { /* corrupt presentation cache: pick a new selection */ }
    const chosen = perspectives[(hash(user.id) + Math.floor(Date.parse(`${clock.dayKey}T00:00:00Z`) / 86400000)) % perspectives.length];
    if (chosen) localStorage.setItem(pickKey, JSON.stringify({ id: chosen.id, text: chosen.text }));
    return chosen;
  }, [perspectives, clock.dayKey, user.id, user.tenant_id]);
  function toggle() { setExpanded(v => !v); setIntro(false); }
  return <section ref={card} className={`cb-perspective ${expanded ? "is-expanded" : "is-compact"}`} onMouseEnter={() => setInteracting(true)} onMouseLeave={() => setInteracting(false)} onFocusCapture={() => setInteracting(true)} onBlurCapture={e => { if (!e.currentTarget.contains(e.relatedTarget)) setInteracting(false); }}>
    <div className="cb-perspective-line"><div className="cb-perspective-icon"><Lightbulb size={24}/></div>
      <div className="cb-perspective-message"><strong>Today's Perspective</strong><span>{selected?.text || "A thoughtful question can open a new path forward."}</span>{expanded && <small>One thoughtful idea for your working day.</small>}</div>
      <button type="button" className="cb-perspective-expand" aria-label={expanded ? "Collapse today's perspective" : "Expand today's perspective"} aria-expanded={expanded} onClick={toggle}>{expanded ? <ChevronUp size={17}/> : <ChevronDown size={17}/>}</button></div>
    <div className="cb-perspective-details" aria-hidden={!expanded}><img src="/perspective-landscape.svg" alt="" loading="lazy"/><button type="button" className="cb-btn cb-btn-sm cb-perspective-refresh" disabled title="The perspective remains the same throughout the day">Daily</button></div>
  </section>;
}
export function BirthdayCelebration({ firstName, onClose, workspaceLogo = "", wish = "" }) {
  const dialog = useRef(null), prior = useRef(null);
  const particles = useMemo(() => Array.from({ length: 54 }, (_, i) => ({ left: `${(i * 71.37) % 100}%`, delay: `${(i % 12) * .23}s`, duration: `${5.2 + (i % 6) * .62}s`, tint: ["#b89043", "#d4ab62", "#a1bfa9", "#24573e"][i % 4], size: 4 + i % 5 })), []);
  useEffect(() => { prior.current = document.activeElement; dialog.current?.focus(); const esc = e => {
    if (e.key === "Escape") onClose();
    if (e.key !== "Tab" || !dialog.current) return;
    const controls = [...dialog.current.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])')];
    if (!controls.length) { e.preventDefault(); return; }
    const first = controls[0], last = controls[controls.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }; window.addEventListener("keydown", esc); return () => { window.removeEventListener("keydown", esc); prior.current?.focus?.(); }; }, [onClose]);
  return createPortal(<div className="cb-birthday-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
    <div className="cb-birthday-particles" aria-hidden="true">{particles.map((p, i) => <i key={i} style={{ left: p.left, animationDelay: p.delay, animationDuration: p.duration, background: p.tint, width: p.size, height: p.size }}/>)}</div>
    <div className="cb-birthday-modal" role="dialog" aria-modal="true" aria-labelledby="cb-birthday-title" ref={dialog} tabIndex={-1}>
      <button className="cb-birthday-close" aria-label="Close birthday celebration" onClick={onClose}><X size={23}/></button>
      <div className="cb-birthday-copy"><div className="cb-birthday-eyebrow">A LITTLE CELEBRATION FROM CLOCKBOOK</div>{workspaceLogo && <img className="cb-birthday-workspace-logo" src={workspaceLogo} alt="Your workspace logo"/>}<h2 id="cb-birthday-title">Happy Birthday,<br/>{firstName}! 🎉</h2><p>{wish || "Wishing you a wonderful year filled with happiness, growth, and meaningful moments."}</p>
      <div className="cb-birthday-actions"><button className="cb-birthday-start" onClick={onClose}><Sun size={20}/> Start my day</button><button className="cb-birthday-dismiss" onClick={onClose}>Dismiss</button></div></div>
      <div className="cb-birthday-art" aria-hidden="true"><img src="/birthday-celebration.webp" alt=""/></div>
    </div>
  </div>, document.body);
}
export function useBirthday(user, clock) {
  const [details, setDetails] = useState(null);
  const [open, setOpen] = useState(false);
  useEffect(() => { let active = true; setDetails(null); api.getBirthday(user.id).then(v => { if (active) setDetails(v); }).catch(() => {}); return () => { active = false; }; }, [user.id, user.tenant_id]);
  const leap = clock.year % 4 === 0 && (clock.year % 100 !== 0 || clock.year % 400 === 0);
  const isBirthday = Boolean(details?.enabled && details.month && details.day && (details.month === clock.month && details.day === clock.day || details.month === 2 && details.day === 29 && !leap && clock.month === 2 && clock.day === 28));
  useEffect(() => { setOpen(isBirthday && details?.acknowledged_year !== clock.year); }, [isBirthday, details?.acknowledged_year, clock.year]);
  async function dismiss() { setOpen(false); try { const updated = await api.acknowledgeBirthday(); setDetails(updated); } catch { /* acknowledgement can be retried on next visit */ } }
  return { isBirthday, birthdayOpen: open, dismissBirthday: dismiss, wish: details?.wish || "" };
}
export function BirthdayPreferences({ member, onClose }) {
  const [value, setValue] = useState({ month: null, day: null, enabled: true }), [error, setError] = useState(""), [busy, setBusy] = useState(true);
  useEffect(() => { api.getBirthday(member.id).then(v => { setValue(v); setBusy(false); }).catch(e => { setError(e.message); setBusy(false); }); }, [member.id]);
  async function save() { setBusy(true); setError(""); try { await api.saveBirthday(member.id, { month: value.month, day: value.day, enabled: value.enabled }); onClose(); } catch (e) { setError(e.message); } finally { setBusy(false); } }
  return createPortal(<div className="cb-birthday-settings-backdrop" onMouseDown={e => { if(e.target === e.currentTarget) onClose(); }}><div className="cb-birthday-settings" role="dialog" aria-modal="true" aria-label={`Birthday preferences for ${member.name}`}><strong>Birthday · {member.name}</strong><p className="cb-hint">Optional and private. Only month and day are saved. February 29 is celebrated on February 28 in non-leap years.</p>
    <div className="cb-birthday-date"><label>Month<select className="cb-select" value={value.month || ""} onChange={e => setValue(v => ({ ...v, month: e.target.value ? +e.target.value : null }))}><option value="">Not set</option>{Array.from({ length: 12 }, (_, i) => <option key={i} value={i+1}>{new Date(2000, i, 1).toLocaleDateString("en", { month: "long" })}</option>)}</select></label><label>Day<select className="cb-select" value={value.day || ""} onChange={e => setValue(v => ({ ...v, day: e.target.value ? +e.target.value : null }))}><option value="">Not set</option>{Array.from({ length: 31 }, (_, i) => <option key={i + 1} value={i + 1}>{i + 1}</option>)}</select></label></div>
    <label className="cb-birthday-checkbox"><input type="checkbox" checked={value.enabled} onChange={e => setValue(v => ({...v, enabled:e.target.checked}))}/> Show birthday celebrations</label>
    {error && <div className="cb-error">{error}</div>}<div className="cb-birthday-actions"><button type="button" className="cb-birthday-save" onClick={save} disabled={busy}>{busy ? "Loading..." : "Save birthday"}</button><button className="cb-btn" onClick={onClose}>Cancel</button></div></div></div>, document.body);
}
export function PerspectiveManager() {
  const [items, setItems] = useState([]), [newText, setNewText] = useState(""), [error, setError] = useState("");
  useEffect(() => { api.managePerspectives().then(setItems).catch(e => setError(e.message)); }, []);
  async function update(item, patch) { try { const result = await api.updatePerspective(item.id, { text: item.text, enabled: item.enabled, ...patch }); setItems(v => v.map(x => x.id === item.id ? result : x)); } catch(e) { setError(e.message); } }
  async function add() { try { const result = await api.createPerspective({ text: newText.trim(), enabled: true }); setItems(v => [...v, result]); setNewText(""); } catch(e) { setError(e.message); } }
  return <details className="cb-tmpl-card cb-perspective-manager"><summary className="cb-perspective-manager-toggle">Daily perspectives <ChevronDown size={18}/></summary><p className="cb-hint">Manage the curated library. Changes affect future daily selections.</p><div className="cb-perspective-create"><textarea className="cb-input" value={newText} maxLength={500} placeholder="Write a thoughtful perspective" onChange={e => setNewText(e.target.value)}/><button className="cb-btn cb-btn-primary" disabled={newText.trim().length < 10} onClick={add}>Add perspective</button></div>{error && <div className="cb-error">{error}</div>}<div className="cb-perspective-manage-list">{items.map(item => <PerspectiveEditor key={item.id} item={item} onSave={update}/>)}</div></details>;
}
function PerspectiveEditor({ item, onSave }) { const [text, setText] = useState(item.text); useEffect(() => setText(item.text), [item.text]); return <div className="cb-perspective-editor"><textarea className="cb-input" maxLength={500} value={text} onChange={e => setText(e.target.value)}/><label><input type="checkbox" checked={item.enabled} onChange={e => onSave(item, { enabled: e.target.checked })}/> Enabled</label><button className="cb-btn cb-btn-sm" disabled={text.trim().length < 10 || text === item.text} onClick={() => onSave(item, { text: text.trim() })}>Save</button></div>; }

export function BirthdayWishManager() {
  const [items, setItems] = useState([]), [newText, setNewText] = useState(""), [error, setError] = useState("");
  useEffect(() => { api.manageBirthdayWishes().then(setItems).catch(e => setError(e.message)); }, []);
  async function update(item, patch) { try { const result = await api.updateBirthdayWish(item.id, { text: item.text, enabled: item.enabled, ...patch }); setItems(v => v.map(x => x.id === item.id ? result : x)); } catch (e) { setError(e.message); } }
  async function add() { try { const result = await api.createBirthdayWish({ text: newText.trim(), enabled: true }); setItems(v => [...v, result]); setNewText(""); } catch (e) { setError(e.message); } }
  return <details className="cb-tmpl-card cb-perspective-manager"><summary className="cb-perspective-manager-toggle">Birthday wishes <ChevronDown size={18}/></summary><p className="cb-hint">Manage the annual birthday messages for this workspace. New messages can be added or existing ones enabled, disabled, or edited.</p><div className="cb-perspective-create"><textarea className="cb-input" value={newText} maxLength={350} placeholder="Write a warm birthday wish" onChange={e => setNewText(e.target.value)}/><button className="cb-btn cb-btn-primary" disabled={newText.trim().length < 10} onClick={add}>Add birthday wish</button></div>{error && <div className="cb-error">{error}</div>}<div className="cb-perspective-manage-list">{items.map(item => <PerspectiveEditor key={item.id} item={item} onSave={update}/>)}</div></details>;
}
