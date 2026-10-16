// Runs without adding frontend dependencies; uses the Node TypeScript parser
// solely for source syntax validation and testing the pure date helpers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let ts;
try { ts = require('typescript'); }
catch { ts = require('/opt/nvm/versions/node/v22.16.0/lib/node_modules/typescript'); }
const dir = require('node:path').resolve(__dirname, '..', 'src');
for (const f of ['App.jsx', 'DashboardEnhancements.jsx']) {
  const sf = ts.createSourceFile(f, fs.readFileSync(`${dir}/${f}`, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.JSX);
  assert.equal(sf.parseDiagnostics.length, 0, `${f} has JSX syntax errors`);
}
const source = fs.readFileSync(`${dir}/DashboardEnhancements.jsx`, 'utf8');
const sf = ts.createSourceFile('DashboardEnhancements.jsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JSX);
const functions = sf.statements.filter(s => ts.isFunctionDeclaration(s) && ['userLocalClock', 'greetingForHour'].includes(s.name?.text)).map(s => s.getText(sf).replace(/^export\s+/, ''));
const context = vm.createContext({ Intl, Date });
vm.runInContext(`${functions.join('\n')}\nthis.local = userLocalClock; this.greeting = greetingForHour;`, context);
assert.equal(context.greeting(5), 'Good morning');
assert.equal(context.greeting(11), 'Good morning');
assert.equal(context.greeting(12), 'Good afternoon');
assert.equal(context.greeting(16), 'Good afternoon');
assert.equal(context.greeting(17), 'Good evening');
assert.equal(context.greeting(4), 'Good evening');
const at = new Date('2026-10-16T00:30:00.000Z');
assert.equal(context.local(at, 'Asia/Colombo').dayKey, '2026-10-16');
assert.equal(context.local(at, 'America/Los_Angeles').dayKey, '2026-10-15');
assert.equal(context.local(new Date('2026-03-29T01:30:00.000Z'), 'Europe/Dublin').hour, 2);
console.log('JSX syntax, greeting boundaries, midnight rollover and timezone checks passed');
