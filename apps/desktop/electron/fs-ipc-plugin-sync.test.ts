import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, expect, it, vi } from 'vitest'

const handlers = vi.hoisted(() => new Map<string, () => Promise<unknown>>())
vi.mock('electron', () => ({
  ipcMain: { handle: (name: string, callback: () => Promise<unknown>) => handlers.set(name, callback) },
  shell: {}
}))

import * as roots from './desktop-plugins-root'
import { registerFsIpc } from './fs-ipc'

let home: string | undefined

afterEach(() => {
  vi.restoreAllMocks()
  handlers.clear()

  if (home) {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

it('serializes root discovery, explicit reconcile and migration, and retries after a failed copy', async () => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-dp-sync-'))
  const root = path.join(home, 'desktop-plugins')
  const source = path.join(home, 'plugins', 'voice', 'desktop', 'plugin.js')
  const legacy = path.join(home, 'profiles', 'coder', 'desktop-plugins', 'legacy', 'plugin.js')

  for (const file of [source, legacy]) {
    fs.mkdirSync(path.dirname(file), { recursive: true })
    fs.writeFileSync(file, 'original')
  }

  fs.mkdirSync(root)
  // Resolve this non-mutating lookup immediately so overlapping IPC requests
  // reach the held migration deterministically, without filesystem timing.
  vi.spyOn(roots, 'ensureDir').mockResolvedValue(root)
  const migrate = roots.migrateProfileScopedDesktopPlugins
  let entered!: () => void
  let release!: () => void

  const migrating = new Promise<void>(resolve => {
    entered = resolve
  })

  const barrier = new Promise<void>(resolve => {
    release = resolve
  })

  const migration = vi.spyOn(roots, 'migrateProfileScopedDesktopPlugins').mockImplementationOnce(async (...args) => {
    const moved = await migrate(...args)
    entered()
    await barrier

    return moved
  })

  const reconcile = vi.spyOn(roots, 'reconcileUnifiedDesktopHalves')
  const copy = vi.spyOn(fs.promises, 'cp')
  registerFsIpc({
    hermesHome: home,
    readActiveDesktopProfile: () => null,
    expandUserPath: value => value,
    resolveRequestedPathForIpc: value => value,
    directoryExists: fs.existsSync,
    resolveGitBinary: () => 'git'
  })
  const lookup = handlers.get('hermes:fs:desktopPluginsRoot')!
  const sync = handlers.get('hermes:fs:reconcileDesktopPlugins')!
  const first = lookup()
  await migrating
  const second = lookup()
  const explicit = sync()

  try {
    await Promise.resolve()
    await Promise.resolve()
    expect(migration).toHaveBeenCalledOnce()
    expect(reconcile).not.toHaveBeenCalled()
  } finally {
    release()
    await Promise.allSettled([first, second, explicit])
  }

  expect(await Promise.all([first, second, explicit])).toEqual([root, root, []])
  expect(copy).toHaveBeenCalledOnce()
  expect(fs.readFileSync(path.join(root, 'legacy', 'plugin.js'), 'utf8')).toBe('original')

  fs.writeFileSync(source, 'updated')
  const future = new Date(Date.now() + 60_000)
  fs.utimesSync(source, future, future)
  copy.mockRejectedValueOnce(new Error('fixture copy failure'))
  const failure = expect(lookup()).rejects.toThrow('fixture copy failure')
  const retry = sync()
  await failure
  expect(await retry).toEqual([path.join(root, 'voice')])
  expect(fs.readFileSync(path.join(root, 'voice', 'plugin.js'), 'utf8')).toBe('updated')
  const marker = JSON.parse(fs.readFileSync(path.join(root, 'voice', roots.PACKAGE_MARKER), 'utf8'))
  expect(marker.sourceMtimeMs).toBe(fs.statSync(source).mtimeMs)
  expect(await lookup()).toBe(root)
  expect(copy).toHaveBeenCalledTimes(3)
})
