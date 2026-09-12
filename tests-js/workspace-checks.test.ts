import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { delimiter, join } from 'node:path'

import { afterEach, expect, it } from 'vitest'

const runner = new URL('../.github/scripts/run-workspace-checks.mjs', import.meta.url).href
const fixtures: string[] = []
const payload = 'x'.repeat(1024 * 1024)

interface CheckResult {
  code: number | null
  stderr: string
  stdout: string
}

function runChecks(empty = false): Promise<CheckResult> {
  const fixture = mkdtempSync(join(tmpdir(), 'hermes-workspace-checks-'))
  fixtures.push(fixture)

  const npmSource = `
const units = ${JSON.stringify(empty ? [] : [
    { location: 'fixture/fail', scripts: { check: 'fixture' } },
    { location: 'fixture/pass', scripts: { check: 'fixture' } },
  ])}
if (process.argv[2] === 'query') {
  console.log(JSON.stringify(units))
} else if (process.argv.includes('fixture/fail')) {
  console.log('x'.repeat(${payload.length}))
  console.log('FAILURE_DETAIL_END')
  process.exitCode = 1
} else {
  console.log('REMAINING_UNIT_END')
}
`

  const fakeNpm = join(fixture, 'npm.mjs')
  writeFileSync(fakeNpm, npmSource)

  if (process.platform === 'win32') {
    writeFileSync(join(fixture, 'npm.cmd'), `@echo off\r\n"${process.execPath}" "${fakeNpm}" %*\r\n`)
  } else {
    writeFileSync(join(fixture, 'npm'), `#!/usr/bin/env node\n${npmSource}`, { mode: 0o755 })
  }

  const launcher = join(fixture, 'run.mjs')
  // Pipes can drain asynchronously. Model that on every host, including Windows,
  // whose native stdout pipes are synchronous, so forced exit loses pending writes.
  writeFileSync(launcher, `
import { Console } from 'node:console'
import { Writable } from 'node:stream'
const output = process.stdout
const delayed = new Writable({
  write(chunk, encoding, callback) {
    setTimeout(() => output.write(chunk, encoding, callback), 10)
  },
})
Object.defineProperty(process, 'stdout', { value: delayed })
globalThis.console = new Console({ stdout: delayed, stderr: process.stderr })
await import(${JSON.stringify(runner)})
`)
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.toLowerCase() !== 'path'))
  env.PATH = `${fixture}${delimiter}${process.env.PATH ?? process.env.Path ?? ''}`
  env.GITHUB_ACTIONS = 'true'

  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [launcher, '--concurrency', '1'], {
      cwd: fixture,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 10_000,
    })

    let stdout = ''
    let stderr = ''
    child.stdout.setEncoding('utf8').on('data', (chunk: string) => { stdout += chunk })
    child.stderr.setEncoding('utf8').on('data', (chunk: string) => { stderr += chunk })
    child.once('error', reject)
    child.once('close', (code) => resolve({ code, stderr, stdout }))
  })
}

afterEach(() => {
  for (const fixture of fixtures.splice(0)) {
    rmSync(fixture, { force: true, recursive: true })
  }
})

it('drains failed-check output and reports all remaining checks before exiting', async () => {
  const { code, stderr, stdout } = await runChecks()
  expect(code).toBe(1)
  expect(stdout.includes(payload)).toBe(true)
  expect(stdout.includes('FAILURE_DETAIL_END')).toBe(true)
  expect(stdout.includes('REMAINING_UNIT_END')).toBe(true)
  expect(stdout.includes('=== summary ===')).toBe(true)
  expect(stderr).toContain('1 of 2 checks failed')
  expect(stdout.includes('all 2 checks passed')).toBe(false)
}, 15_000)

it('fails without declaring success when no workspace supplies a check', async () => {
  const { code, stderr, stdout } = await runChecks(true)
  expect(code).toBe(1)
  expect(stderr).toContain('No workspace package declares a check script')
  expect(stdout).not.toContain('checks passed')
}, 15_000)
