import { useEffect, useState, useRef } from "react";
import { createPortal } from "react-dom";
import { X, Snowflake, ChevronDown } from "lucide-react";
import { userLocalClock } from "./DashboardEnhancements.jsx";
import "./seasonalCelebrations.css";

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
export function SeasonalExperience({ user, workspaceId, logo = "", now, birthdayIsOpen = false, preview = null, onPreviewClose }) {
  const clock = userLocalClock(new Date(now), user.timezone_name);
  const occasion = seasonalOccasion(clock);
  const [dismissed, setDismissed] = useState(false);
  const [snowEnabled, setSnowEnabled] = useState(true);
  const [eventsEnabled, setEventsEnabled] = useState(true);
  const [reducedSnow, setReducedSnow] = useState(false);
  const key = storageKey(user, workspaceId, "prefs");
  const acknowledged = storageKey(user, workspaceId, `ack:${clock.year}:${occasion}`);
  useEffect(() => {
    try { const data = JSON.parse(localStorage.getItem(key) || "{}"); setSnowEnabled(data.snow !== false); setEventsEnabled(data.events !== false); }
    catch { setSnowEnabled(true); setEventsEnabled(true); }
  }, [key]);
  useEffect(() => { try { setDismissed(localStorage.getItem(acknowledged) === "1"); } catch { setDismissed(false); } }, [acknowledged]);
  useEffect(() => { setReducedSnow(false); const timeout = window.setTimeout(() => setReducedSnow(true), 30000); return () => window.clearTimeout(timeout); }, [clock.dayKey, user.id, workspaceId]);
  const showSnow = clock.month === 12 && snowEnabled;
  const activeModal = preview || (eventsEnabled && !birthdayIsOpen && occasion && !dismissed ? occasion : null);
  function dismiss() {
    if (preview) { onPreviewClose?.(); return; }
    setDismissed(true);
    try { localStorage.setItem(acknowledged, "1"); } catch { /* presentation must not prevent working */ }
  }
  return <>
    {showSnow && <div className={`cb-season-snow ${reducedSnow ? "cb-season-snow-subtle" : ""}`} aria-hidden="true">{Array.from({ length: 14 }, (_, i) => <span key={i} style={{ left: `${3 + (i * 37 % 94)}%`, animationDelay: `${-i * 1.8}s`, animationDuration: `${16 + i % 7 * 3}s` }}>❄</span>)}</div>}
    {activeModal && <SeasonalCelebration occasion={activeModal} name={(user.name || "there").trim().split(/\s+/)[0]} logo={logo} onClose={dismiss}/>}
  </>;
}
export function SeasonalSettings({ user, workspaceId, onPreview }) {
  const key = storageKey(user, workspaceId, "prefs");
  const [prefs, setPrefs] = useState({ snow: true, events: true });
  useEffect(() => { try { const existing = JSON.parse(localStorage.getItem(key) || "{}"); setPrefs({ snow: existing.snow !== false, events: existing.events !== false }); } catch { setPrefs({ snow: true, events: true }); } }, [key]);
  function update(field, checked) { const next = { ...prefs, [field]: checked }; setPrefs(next); try { localStorage.setItem(key, JSON.stringify(next)); window.dispatchEvent(new Event("clockbook-seasonal-settings")); } catch { /* optional preferences */ } }
  return <details className="cb-tmpl-card cb-season-settings">
    <summary className="cb-perspective-manager-toggle">Seasonal celebrations <ChevronDown size={18}/></summary>
    <p className="cb-hint">Personal preferences for this workspace on this browser. Reduced-motion accessibility settings always take precedence.</p>
    <label><input type="checkbox" checked={prefs.snow} onChange={e => update("snow", e.target.checked)}/> Show gentle December snowfall</label>
    <label><input type="checkbox" checked={prefs.events} onChange={e => update("events", e.target.checked)}/> Show seasonal greetings</label>
    <div className="cb-season-preview-buttons">{Object.entries(SEASONS).map(([id, occasion]) => <button type="button" key={id} className="cb-btn cb-btn-sm" onClick={() => onPreview(id)}>Preview {occasion.label}</button>)}</div>
  </details>;
}
