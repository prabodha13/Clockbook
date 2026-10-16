import { useEffect, useState, useRef } from "react";
import { createPortal } from "react-dom";
import { X, Snowflake, ChevronDown } from "lucide-react";
import { userLocalClock } from "./DashboardEnhancements.jsx";
import "./seasonalCelebrations.css";
import { api } from "./api.js";

export const SEASONS = {
  halloween: { label: "Halloween", title: "Happy Halloween", message: "May your day bring a little mystery, plenty of laughter, and no frightening deadlines.", symbol: "🎃" },
  christmas: { label: "Christmas", title: "Merry Christmas", message: "Wishing you a peaceful festive season filled with warmth, gratitude, and joyful moments.", symbol: "🎄" },
  yearend: { label: "Year End", title: "Thank you for a wonderful year", message: "Here's to the people, progress, and meaningful moments that made this year memorable.", symbol: "✨" },
  newyear: { label: "New Year", title: "Happy New Year", message: "Wishing you a fresh year of opportunity, growth, and shared success.", symbol: "✨" },
};

export function seasonalOccasion(clock) {
  if (clock.month === 10 && clock.day === 31) return "halloween";
  if (clock.month === 12 && clock.day >= 24 && clock.day <= 26) return "christmas";
  if (clock.month === 12 && clock.day === 31) return "yearend";
  if (clock.month === 1 && clock.day === 1) return "newyear";
  return null;
}
const storageKey = (user, workspaceId, suffix) => `clockbook:seasonal:${workspaceId || user.tenant_id || "workspace"}:${user.id}:${suffix}`;
export function SeasonalCelebration({ occasion, name, logo, onClose }) {
  const modalRef = useRef(null);
  const previous = useRef(null);
  const item = SEASONS[occasion];
  useEffect(() => {
    previous.current = document.activeElement;
    modalRef.current?.focus();
    function onKey(e) {
      if (e.key === "Escape") onClose();
      if (e.key !== "Tab" || !modalRef.current) return;
      const buttons = Array.from(modalRef.current.querySelectorAll("button:not(:disabled)"));
      if (!buttons.length) return;
      if (e.shiftKey && document.activeElement === buttons[0]) { e.preventDefault(); buttons[buttons.length - 1].focus(); }
      if (!e.shiftKey && document.activeElement === buttons[buttons.length - 1]) { e.preventDefault(); buttons[0].focus(); }
    }
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); previous.current?.focus?.(); };
  }, [onClose]);
  if (!item) return null;
  return createPortal(<div className="cb-season-overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
    <section className={`cb-season-modal cb-season-${occasion}`} role="dialog" aria-modal="true" aria-labelledby="cb-season-title" ref={modalRef} tabIndex={-1}>
      <button className="cb-season-close" aria-label="Close seasonal greeting" onClick={onClose}><X size={19}/></button>
      {logo && <img className="cb-season-logo" src={logo} alt="Workspace logo"/>}
      <div className="cb-season-illustration" aria-hidden="true">{item.symbol}</div>
      <div className="cb-season-eyebrow">A SEASONAL WISH FROM YOUR WORKSPACE</div>
      <h2 id="cb-season-title">{item.title}{occasion !== "yearend" ? `, ${name}!` : ""}</h2>
      <p>{item.message}</p>
      <button className="cb-season-continue" onClick={onClose}>Continue to ClockBook</button>
    </section>
  </div>, document.body);
}
const SEASON_DEFAULTS = { halloween: true, christmas: true, yearend: true, newyear: true, snow: true };
const snowKey = (user, workspaceId) => storageKey(user, workspaceId, "snow-opt-out");
function Snowflakes({ subtle = false, preview = false }) {
  const count = preview ? 30 : 14;
  return <div className={`cb-season-snow ${subtle ? "cb-season-snow-subtle" : ""} ${preview ? "cb-season-snow-preview" : ""}`} aria-hidden="true">{Array.from({ length: count }, (_, i) => <span key={i} style={{ left: `${3 + (i * 37 % 94)}%`, animationDelay: `${-i * 1.8}s`, animationDuration: `${16 + i % 7 * 3}s` }}>❄</span>)}</div>;
}
export function SnowfallPreview({ onClose }) {
  const [reducedSnow, setReducedSnow] = useState(false);
  useEffect(() => {
    const timeout = window.setTimeout(() => setReducedSnow(true), 30000);
    return () => window.clearTimeout(timeout);
  }, []);
  return <><Snowflakes preview subtle={reducedSnow}/><div className="cb-snow-preview-control"><button type="button" className="cb-btn cb-btn-primary" onClick={onClose}>Close snowfall preview</button></div></>;
}
export function SeasonalExperience({ user, workspaceId, logo = "", now, birthdayIsOpen = false, preview = null, onPreviewClose }) {
  const clock = userLocalClock(new Date(now), user.timezone_name);
  const occasion = seasonalOccasion(clock);
  const [dismissed, setDismissed] = useState(false);
  const [prefs, setPrefs] = useState(null);
  const [optOut, setOptOut] = useState(false);
  const [reducedSnow, setReducedSnow] = useState(false);
  const acknowledged = storageKey(user, workspaceId, `ack:${clock.year}:${occasion}`);
  useEffect(() => {
    let active = true;
    api.getSeasonalSettings().then(data => { if (active) setPrefs({ ...SEASON_DEFAULTS, ...data }); }).catch(() => { if (active) setPrefs(null); });
    const refresh = () => api.getSeasonalSettings().then(data => { if (active) setPrefs({ ...SEASON_DEFAULTS, ...data }); }).catch(() => {});
    window.addEventListener("clockbook-seasonal-settings", refresh);
    return () => { active = false; window.removeEventListener("clockbook-seasonal-settings", refresh); };
  }, [workspaceId, user.tenant_id]);
  useEffect(() => { try { setOptOut(localStorage.getItem(snowKey(user, workspaceId)) === "1"); } catch { setOptOut(false); } }, [workspaceId, user.id]);
  useEffect(() => { try { setDismissed(localStorage.getItem(acknowledged) === "1"); } catch { setDismissed(false); } }, [acknowledged]);
  useEffect(() => { setReducedSnow(false); const timeout = window.setTimeout(() => setReducedSnow(true), 30000); return () => window.clearTimeout(timeout); }, [clock.dayKey, user.id, workspaceId]);
  const showSnow = clock.month === 12 && prefs?.snow && !optOut;
  const activeModal = preview || (!birthdayIsOpen && occasion && prefs?.[occasion] && !dismissed ? occasion : null);
  function dismiss() {
    if (preview) { onPreviewClose?.(); return; }
    setDismissed(true);
    try { localStorage.setItem(acknowledged, "1"); } catch { /* presentation must not prevent working */ }
  }
  return <>
    {showSnow && <Snowflakes subtle={reducedSnow}/>}
    {showSnow && <button className="cb-snow-optout" type="button" onClick={() => { try { localStorage.setItem(snowKey(user, workspaceId), "1"); } catch {} setOptOut(true); }}>Hide snow</button>}
    {clock.month === 12 && prefs?.snow && optOut && <button className="cb-snow-optout" type="button" onClick={() => { try { localStorage.removeItem(snowKey(user, workspaceId)); } catch {} setOptOut(false); }}>Show snow</button>}
    {activeModal && <SeasonalCelebration occasion={activeModal} name={(user.name || "there").trim().split(/\s+/)[0]} logo={logo} onClose={dismiss}/>}
  </>;
}
export function SeasonalSettings() {
  const [prefs, setPrefs] = useState(SEASON_DEFAULTS);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => { api.getSeasonalSettings().then(v => setPrefs({ ...SEASON_DEFAULTS, ...v })).catch(e => setError(e.message)); }, []);
  async function update(field, checked) {
    const next = { ...prefs, [field]: checked };
    setSaving(true); setError("");
    try { const saved = await api.updateSeasonalSettings(next); setPrefs(saved); window.dispatchEvent(new Event("clockbook-seasonal-settings")); }
    catch (e) { setError(e.message); } finally { setSaving(false); }
  }
  return <details className="cb-tmpl-card cb-perspective-manager cb-season-settings">
    <summary className="cb-perspective-manager-toggle">Seasonal celebrations <ChevronDown size={18}/></summary>
    <p className="cb-hint">Workspace-wide controls. Staff can individually hide December snowfall on their dashboard.</p>
    {Object.entries({ halloween: "Halloween", christmas: "Christmas", yearend: "Year End", newyear: "New Year", snow: "December snowfall" }).map(([id, label]) => <label key={id}><input type="checkbox" disabled={saving} checked={prefs[id]} onChange={e => update(id, e.target.checked)}/> Enable {label}</label>)}
    {error && <div role="alert" className="cb-error">{error}</div>}
  </details>;
}
