// Build an isolated, clean FE main checkout without loading its .env files.
import { execFileSync } from 'node:child_process'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { pathToFileURL } from 'node:url'

const [sourceArg, outputArg] = process.argv.slice(2)
if (!sourceArg || !outputArg) {
  throw new Error('Usage: node build_frontend.mjs <clean-FE-main-checkout> <new-output-directory>')
}
const source = resolve(sourceArg)
const output = resolve(outputArg)
const withinSource = relative(source, output)
if (!withinSource || (!withinSource.startsWith(`..${sep}`) && withinSource !== '..')) {
  throw new Error('Use a new output directory outside the FE checkout.')
}
const git = (...args) => execFileSync('git', ['-C', source, ...args], { encoding: 'utf8' }).trim()
const sha = git('rev-parse', 'HEAD')
if (git('branch', '--show-current') !== 'main' || sha !== git('rev-parse', 'origin/main') || git('status', '--porcelain')) {
  throw new Error('Fetch and use a clean main checkout at origin/main; preserve user changes elsewhere.')
}
// Refuse to empty or overwrite an existing build.
await mkdir(output)
const envDir = await mkdtemp(join(tmpdir(), 'visible-lecture-empty-env-'))
try {
  for (const name of Object.keys(process.env)) {
    if (name.startsWith('VITE_')) delete process.env[name]
  }
  process.env.VITE_USE_MOCK_API = 'false'
  process.env.VITE_API_BASE_URL = ''
  const { build } = await import(pathToFileURL(join(source, 'node_modules/vite/dist/node/index.js')))
  await build({
    root: source,
    mode: 'production',
    base: '/',
    envDir,
    define: {
      'import.meta.env.VITE_USE_MOCK_API': JSON.stringify('false'),
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify(''),
    },
    build: { outDir: output, emptyOutDir: false, sourcemap: false },
  })
  await readFile(join(output, 'index.html'))
  await writeFile(join(dirname(output), `${output.split(sep).pop()}.manifest.json`), JSON.stringify({
    fe_commit: sha,
    VITE_USE_MOCK_API: 'false',
    VITE_API_BASE_URL: '',
    note: 'Recheck required PR merges before final deployment.',
  }, null, 2) + '\n', { flag: 'wx' })
  console.log(`Built FE main ${sha}; real /api requests use the same origin.`)
} finally {
  await rm(envDir, { recursive: true, force: true })
}
