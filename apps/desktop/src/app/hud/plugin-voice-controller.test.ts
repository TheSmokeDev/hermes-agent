// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { PluginVoiceDescriptor } from '../../../electron/plugin-voice-contract'

import { createPluginVoiceController } from './plugin-voice-controller'

const gateway = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('@/store/gateway', () => ({ requestGatewayForAgent: gateway.request }))

const descriptor: PluginVoiceDescriptor = {
  id: 'surface-id', pluginId: 'demo-voice',
  owner: { connectionId: 'remote', profile: 'bot', sessionId: 'live', storedSessionId: 'stored' }
}

afterEach(() => {
  gateway.request.mockReset()
  vi.restoreAllMocks()
})

describe('native plugin voice preparation', () => {
  it('keeps the prepared owner when session.activate answers with the live session payload', async () => {
    const calls: string[] = []
    gateway.request.mockImplementation(async (_connection: string, _profile: string, method: string) => {
      calls.push(method)

      if (method === 'session.prepare') {
        return { session_id: 'live', stored_session_id: 'stored' }
      }

      if (method === 'session.activate') {
        // The gateway's live session payload: session_id and session_key, never stored_session_id.
        return { session_id: 'live', session_key: 'stored', info: { model: 'demo' }, message_count: 0 }
      }

      throw new Error(`unexpected ${method}`)
    })
    const stop = vi.fn()
    Object.assign(window, { hermesDesktop: { hud: { voice: { stop } } } })

    const { controller } = createPluginVoiceController(descriptor)

    await expect(controller.prepareSession()).resolves.toEqual(descriptor.owner)
    expect(calls).toEqual(['session.prepare', 'session.activate'])
    expect(stop).not.toHaveBeenCalled()
    expect(controller.signal.aborted).toBe(false)
  })

  it('stops when session.prepare resolves a different conversation', async () => {
    gateway.request.mockImplementation(async (_connection: string, _profile: string, method: string) =>
      method === 'session.prepare' ? { session_id: 'other', stored_session_id: 'elsewhere' } : {})
    const stop = vi.fn()
    Object.assign(window, { hermesDesktop: { hud: { voice: { stop } } } })

    const { controller } = createPluginVoiceController(descriptor)

    await expect(controller.prepareSession()).rejects.toThrow('Voice owner no longer matches')
    expect(stop).toHaveBeenCalledWith(descriptor.id)
    expect(controller.signal.aborted).toBe(true)
  })
})
