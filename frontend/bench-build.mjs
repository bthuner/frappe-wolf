// Run by `bench build` and `bench setup requirements --node` through the root package.json.
// The Wolf UI needs Node 20.19+. On an older Node it is skipped with a warning instead of
// failing the whole bench build: Desk does not need it, and /wolf then says it is not built.
import { execSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const [major, minor] = process.versions.node.split('.').map(Number)
if (major < 20 || (major === 20 && minor < 19)) {
  console.warn(
    `\nFrappe Wolf: the Wolf UI (/wolf) needs Node 20.19 or newer, this is ${process.versions.node}. ` +
      'Skipping it; Desk is not affected.\n',
  )
  process.exit(0)
}
const run = (cmd) => execSync(cmd, { cwd: here, stdio: 'inherit' })
if (process.argv.includes('--install-only') || !existsSync(`${here}/node_modules`)) {
  run('yarn install --frozen-lockfile --non-interactive')
}
if (!process.argv.includes('--install-only')) run('yarn build')
