import { afterEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/hermes'

import { activeConnection, pluginRest } from './plugins'

// desktop.getConnection/getConnectionFor are IPC round-trips into the main
// process with no timeout of their own (#93454). A wedged main-process
// round-trip must reject instead of hanging pluginSocket's connect() forever.
describe('activeConnection connection timeout (#93454)', () => {
  afterEach(() => {
    setApiRequestConnection(null)
    setApiRequestProfile(null)
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.useRealTimers()
  })

  it('rejects instead of hanging forever when getConnection() wedges', async () => {
    vi.useFakeTimers()
    setApiRequestProfile('coder')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { getConnection: vi.fn(() => new Promise(() => undefined)) }
    })

    const pending = expect(activeConnection()).rejects.toThrow('Timed out connecting to profile "coder"')

    await vi.advanceTimersByTimeAsync(20_000)
    await pending
  })

  it('rejects instead of hanging forever when getConnectionFor() wedges', async () => {
    vi.useFakeTimers()
    setApiRequestConnection('gw-tailscale')
    setApiRequestProfile('research')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnection: vi.fn(() => new Promise(() => undefined)),
        getConnectionFor: vi.fn(() => new Promise(() => undefined))
      }
    })

    const pending = expect(activeConnection()).rejects.toThrow('Timed out connecting to profile "research"')

    await vi.advanceTimersByTimeAsync(20_000)
    await pending
  })
})

describe('pinned plugin REST', () => {
  afterEach(() => {
    setApiRequestConnection(null)
    setApiRequestProfile(null)
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('retains the original route and token through focus changes, including close', async () => {
    const api = vi.fn().mockResolvedValue({ ok: true })
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
    const scope = { connectionId: 'original-host', profile: 'original-profile' }
    await pluginRest('voice', '/session', { scope, pluginToken: 'fixture', method: 'POST' })
    setApiRequestConnection('other-host')
    setApiRequestProfile('other-profile')
    await pluginRest('voice', '/close', { scope, pluginToken: 'fixture', method: 'POST' })
    expect(api.mock.calls.map(([request]) => request)).toEqual([
      expect.objectContaining({ path: '/api/plugins/voice/session', ...scope, pluginToken: 'fixture' }),
      expect.objectContaining({ path: '/api/plugins/voice/close', ...scope, pluginToken: 'fixture' })
    ])
    await pluginRest('voice', '/status')
    expect(api).toHaveBeenLastCalledWith(
      expect.objectContaining({ connectionId: 'other-host', profile: 'other-profile' })
    )
  })

  it('rejects incomplete pins and namespace traversal before IPC without replacing network errors', async () => {
    const failure = new Error('503: backend unavailable')
    const api = vi.fn().mockRejectedValue(failure)
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
    await expect(pluginRest('voice', '/close', { scope: { connectionId: '', profile: 'default' } })).rejects.toThrow(
      'scope requires'
    )
    await expect(pluginRest('voice', '/../other/session')).rejects.toThrow('path traversal')
    expect(api).not.toHaveBeenCalled()
    await expect(pluginRest('voice', '/status')).rejects.toBe(failure)
  })
})
