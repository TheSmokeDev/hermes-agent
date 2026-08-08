import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $voiceConversationStartRequest } from '@/store/composer'
import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId, $gatewayState, $selectedStoredSessionId, $sessions } from '@/store/session'

import { useQuickEntryBridge } from './use-quick-entry-bridge'

const callbacks: { voice?: (payload: unknown) => void } = {}
const pushState = vi.fn()

function installApi() {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      quickEntry: {
        onSubmit: vi.fn(() => () => undefined),
        onVoiceStart: vi.fn(callback => {
          callbacks.voice = callback

          return () => delete callbacks.voice
        }),
        pushState
      }
    }
  })
}

describe('useQuickEntryBridge voice launch', () => {
  beforeEach(() => {
    installApi()
    pushState.mockClear()
    callbacks.voice = undefined
    $activeGatewayProfile.set('ops')
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $gatewayState.set('closed')
    $sessions.set([])
  })

  afterEach(cleanup)

  it('projects unavailable and rejects current voice while gateway is open but no runtime is bound', () => {
    $gatewayState.set('open')
    renderHook(() => useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText: vi.fn() }))

    expect(pushState).toHaveBeenLastCalledWith(
      expect.objectContaining({ connected: true, currentVoiceTargetAvailable: false })
    )

    const before = $voiceConversationStartRequest.get()
    act(() => callbacks.voice?.({ target: 'current' }))
    expect($voiceConversationStartRequest.get()).toBe(before)
  })

  it('snapshots host-owned profile, runtime, and durable identity for a valid current intent', () => {
    $gatewayState.set('open')
    $activeSessionId.set('runtime-1')
    $selectedStoredSessionId.set('stored-1')
    renderHook(() => useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText: vi.fn() }))

    expect(pushState).toHaveBeenLastCalledWith(
      expect.objectContaining({ connected: true, currentVoiceTargetAvailable: true })
    )

    act(() => callbacks.voice?.({ target: 'current', profile: 'attacker', runtimeSessionId: 'wrong' }))

    expect($voiceConversationStartRequest.get()?.binding).toEqual({
      durableSessionId: 'stored-1',
      profile: 'ops',
      runtimeSessionId: 'runtime-1'
    })
  })

  it('rejects malformed and unsupported voice targets', () => {
    $gatewayState.set('open')
    $activeSessionId.set('runtime-1')
    renderHook(() => useQuickEntryBridge({ startFreshSessionDraft: vi.fn(), submitText: vi.fn() }))
    const before = $voiceConversationStartRequest.get()

    act(() => {
      callbacks.voice?.({ target: 'new' })
      callbacks.voice?.({ target: 'stored-1' })
      callbacks.voice?.({})
    })

    expect($voiceConversationStartRequest.get()).toBe(before)
  })
})
