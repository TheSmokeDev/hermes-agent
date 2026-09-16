import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode, useEffect, useState } from 'react'
import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest'

import { acquireMicrophoneLease } from '@/app/chat/composer/hooks/composer-microphone-lease'
import { createPluginContext, type PluginContext } from '@/contrib/plugin'
import type { PluginVoice, PluginVoiceController } from '@/sdk'
import { $pluginVoiceRenderers } from '@/store/plugin-voice'

import { createMicrophoneArbiter } from '../../../electron/microphone-lease'
import type { PluginVoiceDescriptor } from '../../../electron/plugin-voice-contract'

import { PluginVoiceHud } from './plugin-voice-hud'

const gateway = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('@/store/gateway', () => ({ requestGatewayForAgent: gateway.request }))

const descriptor: PluginVoiceDescriptor = {
  id: 'surface-id',
  pluginId: 'demo-voice',
  owner: { connectionId: 'remote', profile: 'bot', sessionId: 'live', storedSessionId: 'stored' }
}

afterEach(() => {
  cleanup()
  $pluginVoiceRenderers.set(new Map())
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  gateway.request.mockReset()
})

function bridgeFixture() {
  const arbiter = createMicrophoneArbiter()
  const stopped = new Set<(id: string) => void>()
  let permission: (() => void) | undefined
  const status = {
    state: 'granted',
    addEventListener: (_event: string, fn: () => void) => {
      permission = fn
    },
    removeEventListener: vi.fn()
  }
  vi.stubGlobal('navigator', { permissions: { query: async () => status } })
  const close = vi.fn()
  const stop = vi.fn((id: string) => stopped.forEach(fn => fn(id)))

  const voice = {
    open: vi.fn(async () => {}),
    get: async () => descriptor,
    stop,
    unregister: vi.fn(),
    onStopped: (fn: (id: string) => void) => {
      stopped.add(fn)

      return () => {
        stopped.delete(fn)
      }
    }
  }

  Object.assign(window, {
    hermesDesktop: {
      hud: { voice, close },
      microphone: {
        acquire: async (token: string) => arbiter.acquire(7, token),
        release: (token: string) => arbiter.release(7, token)
      }
    }
  })
  gateway.request.mockImplementation(async (_connection, _profile, method) =>
    method.startsWith('session.')
      ? { session_id: descriptor.owner.sessionId, stored_session_id: descriptor.owner.storedSessionId }
      : {}
  )

  return {
    arbiter,
    voice,
    stop,
    deny: () => {
      status.state = 'denied'
      permission?.()
    }
  }
}

describe('plugin voice renderer through the public context', () => {
  it('mounts once independently of the launcher and preserves runtime/draft through collapse and reopen', async () => {
    expectTypeOf<NonNullable<PluginContext['voice']>>().toEqualTypeOf<PluginVoice>()
    const { arbiter, voice } = bridgeFixture()
    const context = createPluginContext(descriptor.pluginId)
    let controller!: PluginVoiceController
    let mounts = 0
    let releases = 0
    context.voice!.register(props => {
      controller = props.controller
      const [collapsed, setCollapsed] = useState(false)
      const [draft, setDraft] = useState('')
      useEffect(() => {
        mounts++

        return () => {
          mounts--
        }
      }, [])

      return (
        <>
          <button onClick={() => setCollapsed(value => !value)}>collapse</button>
          <input aria-label="draft" hidden={collapsed} onChange={event => setDraft(event.target.value)} value={draft} />
        </>
      )
    })
    const launcher = render(<button onClick={() => void context.voice!.open(descriptor.owner)}>open</button>)
    fireEvent.click(screen.getByText('open'))
    await waitFor(() =>
      expect(voice.open).toHaveBeenCalledWith({ pluginId: descriptor.pluginId, owner: descriptor.owner })
    )
    launcher.unmount()
    const hud = render(
      <StrictMode>
        <PluginVoiceHud />
      </StrictMode>
    )
    await screen.findByLabelText('draft')
    expect(mounts).toBe(1)
    const lease = await controller.acquire()
    expect(lease).not.toBeNull()
    lease!.signal.addEventListener('abort', () => {
      releases++
    })
    fireEvent.change(screen.getByLabelText('draft'), { target: { value: 'unsent' } })
    fireEvent.click(screen.getByText('collapse'))
    await context.voice!.open(descriptor.owner)
    fireEvent.click(screen.getByText('collapse'))
    expect((screen.getByLabelText('draft') as HTMLInputElement).value).toBe('unsent')
    expect(mounts).toBe(1)
    expect(lease!.signal.aborted).toBe(false)
    expect(arbiter.acquire(8, 'native-dictation')).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(controller.signal.aborted).toBe(true)
    expect(releases).toBe(1)
    await waitFor(() => expect(arbiter.acquire(8, 'native-after-stop')).toBe(true))
    arbiter.releaseWindow(8)
    hud.unmount()
  })

  it('rejects native microphone contention and aborts media on permission loss and actual runtime unmount', async () => {
    const { arbiter, deny, stop } = bridgeFixture()
    const context = createPluginContext(descriptor.pluginId)
    let controller!: PluginVoiceController
    context.voice!.register(props => {
      controller = props.controller

      return <span>ready</span>
    })
    const hud = render(<PluginVoiceHud />)
    await screen.findByText('ready')
    expect(arbiter.acquire(8, 'native')).toBe(true)
    expect(await controller.acquire()).toBeNull()
    arbiter.release(8, 'native')
    const lease = await controller.acquire()
    expect(lease).not.toBeNull()
    act(deny)
    expect(lease!.signal.aborted).toBe(true)
    expect(controller.signal.aborted).toBe(false)
    expect(stop).not.toHaveBeenCalled()
    expect(await controller.acquire()).toBeNull()
    hud.unmount()
    expect(controller.signal.aborted).toBe(true)
    await expect(
      acquireMicrophoneLease({
        owner: Symbol('native'),
        voiceContextIsCurrent: () => false,
        pause: async () => {},
        resume: () => {}
      })
    ).resolves.toBeNull()
  })
})
