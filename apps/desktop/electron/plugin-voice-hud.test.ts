import { describe, expect, it, vi } from 'vitest'

import { buildHudWindowUrl } from './hud-url'
import { createPluginVoiceHud } from './plugin-voice-hud'

const owner = { connectionId: 'local', profile: 'default', sessionId: 'live', storedSessionId: 'stored' }

function fixture(validateOwner = async () => {}) {
  const send = vi.fn()
  let window: { isDestroyed(): boolean; webContents: { id: number; send: typeof send } } | null = null
  const spawn = vi.fn(() => {
    window = { isDestroyed: () => false, webContents: { id: 7, send } }
  })
  const focus = vi.fn()
  const close = vi.fn()
  const hud = createPluginVoiceHud({ getWindow: () => window, spawn, focus, close, validateOwner })

  return {
    hud,
    spawn,
    focus,
    close,
    send,
    clear: () => {
      window = null
      hud.closed()
    }
  }
}

describe('plugin voice HUD ownership', () => {
  it('coalesces pending opens, focuses the exact owner and refuses colliding connection/profile identities', async () => {
    let resolve!: () => void
    const validate = vi.fn(
      () =>
        new Promise<void>(done => {
          resolve = done
        })
    )
    const { hud, spawn, focus, close, clear } = fixture(validate)
    const request = { pluginId: 'voice-plugin', owner }
    const first = hud.open({ ...request, token: 'must-not-cross', owner: { ...owner, secret: 'must-not-cross' } })
    const duplicate = hud.open(request)
    expect(spawn).not.toHaveBeenCalled()

    for (const changed of [
      { connectionId: 'remote' },
      { profile: 'other' },
      { storedSessionId: 'other' },
      { sessionId: 'other' }
    ]) {
      await expect(hud.open({ ...request, owner: { ...owner, ...changed } })).rejects.toThrow('current HUD')
    }

    resolve()
    await Promise.all([first, duplicate])
    await hud.open(request)
    expect(spawn).toHaveBeenCalledOnce()
    expect(validate).toHaveBeenCalledOnce()
    expect(focus).toHaveBeenCalledTimes(2)
    expect(hud.descriptor(99)).toBeNull()
    expect(hud.descriptor(7)).toEqual({ ...request, id: expect.any(String) })
    expect(buildHudWindowUrl(null, { devServer: 'http://localhost:5174', pluginVoice: true })).toBe(
      'http://localhost:5174/?win=hud&voice=1#/'
    )
    hud.stop('stale-id')
    expect(close).not.toHaveBeenCalled()
    hud.stop(hud.current()!.id)
    expect(close).toHaveBeenCalledOnce()
    await expect(hud.open(request)).rejects.toThrow('current HUD')
    clear()
    expect(hud.current()).toBeNull()
  })

  it('retires failures and cancelled pending owners without spawning or poisoning the next open', async () => {
    const validate = vi.fn().mockRejectedValueOnce(new Error('owner missing')).mockResolvedValue(undefined)
    const { hud, spawn, clear } = fixture(validate)
    const request = { pluginId: 'voice-plugin', owner }
    await expect(hud.open(request)).rejects.toThrow('owner missing')
    expect(hud.current()).toBeNull()
    expect(spawn).not.toHaveBeenCalled()
    await hud.open(request)
    clear()
    const opening = hud.open(request)
    hud.stop(hud.current()!.id)
    await expect(opening).rejects.toThrow('invalidated')
    await hud.open({ ...request, owner: { ...owner, connectionId: 'remote', profile: 'bot' } })
    expect(hud.current()?.owner.connectionId).toBe('remote')
  })
})
