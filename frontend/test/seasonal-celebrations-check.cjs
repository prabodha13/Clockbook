const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
let ts;
try { ts = require('typescript'); }
catch { ts = require('/opt/nvm/versions/node/v22.16.0/lib/node_modules/typescript'); }
const dir = path.resolve(__dirname, '..', 'src');
for (const filename of ['App.jsx','SeasonalCelebrations.jsx']) {
  const ast = ts.createSourceFile(filename, fs.readFileSync(path.join(dir, filename), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.JSX);
  assert.equal(ast.parseDiagnostics.length, 0, filename + ': ' + ast.parseDiagnostics.map(x => x.messageText).join(', '));
}
const ast = ts.createSourceFile('SeasonalCelebrations.jsx', fs.readFileSync(path.join(dir, 'SeasonalCelebrations.jsx'),'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.JSX);
const fn = ast.statements.find(stmt => ts.isFunctionDeclaration(stmt) && stmt.name?.text === 'seasonalOccasion');
const context = vm.createContext({});
vm.runInContext(fn.getText(ast).replace(/^export\s+/, '') + ';this.seasonalOccasion=seasonalOccasion;', context);
for (const [m,d,want] of [[10,24,null],[10,31,'halloween'],[12,1,null],[12,24,'christmas'],[12,26,'christmas'],[12,27,null],[12,31,'yearend'],[1,1,'newyear'],[1,2,null]]) {
  assert.equal(context.seasonalOccasion({month:m,day:d}), want);
}
console.log('Seasonal JSX syntax and date boundaries passed');
