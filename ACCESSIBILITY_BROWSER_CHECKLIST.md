# Accessibility and browser validation checklist

This is a practical release checklist, not a claim of WCAG certification.

## Core workflows

Test at minimum:

- login/logout;
- dashboard task search;
- Start/Pause/Resume/Complete/Reset;
- New Task modal;
- workspace selector;
- staff/invitation management;
- clients;
- Settings;
- Insights;
- Calendar/Meeting controls when enabled;
- guided tour.

## Accessibility checks

- all core actions reachable with keyboard only;
- visible focus indicator on interactive elements;
- dialogs trap/restore focus sensibly;
- icon-only controls have accessible labels;
- form controls have programmatic labels;
- status/errors are not communicated by colour alone;
- 100%, 125%, 150% and 200% zoom remain usable;
- no critical action depends on hover;
- tour targets/tooltips remain usable at common zoom levels.

## Browser checks

Run the same smoke suite in current supported Chrome and Edge under normal and restricted-notification conditions.

ClockBook is desktop-first. On tablet/mobile, the acceptance standard is graceful degradation: no privileged action becomes accidentally exposed and the user is not trapped in an unusable state.

Record browser versions, date, tester and failures for each release candidate where material frontend behavior changed.
