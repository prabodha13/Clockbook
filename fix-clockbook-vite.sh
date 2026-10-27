#!/usr/bin/env bash
set -euo pipefail

# Run at the root of the ClockBook repository.
if [ ! -f frontend/package.json ] || [ ! -f frontend/package-lock.json ]; then
  echo 'ERROR: Run this script from the ClockBook repository root.' >&2
  exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo 'ERROR: npm is required.' >&2
  exit 1
fi

cd frontend
printf '\n== Before: Vite version ==\n'
node -p "require('./package-lock.json').packages['node_modules/vite'].version"

printf '\n== Apply non-forced security fixes ==\n'
npm audit fix

printf '\n== Verify security audit ==\n'
npm audit --audit-level=high

printf '\n== Verify Vite is patched ==\n'
node <<'JS'
const lock = require('./package-lock.json');
const version = lock.packages?.['node_modules/vite']?.version;
const match = /^(\d+)\.(\d+)\.(\d+)/.exec(version || '');
if (!match || (Number(match[1]) === 6 && Number(match[2]) === 4 && Number(match[3]) <= 3)) {
  console.error(`ERROR: Vite remains vulnerable or unresolved: ${version}`);
  process.exit(1);
}
console.log(`Locked Vite version: ${version}`);
JS

printf '\n== Verify production frontend build ==\n'
npm run build

printf '\n== Review changed files ==\n'
cd ..
git status --short
git diff -- frontend/package.json frontend/package-lock.json
printf '\nDONE: Review the diff and deploy through your normal process. No commit or push was performed.\n'
