import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { requestVoiceConversationStart } from '@/store/composer'
import { $activeGatewayProfile } from '@/store/profile'
import { $quickEntryVoiceProjection, setQuickEntryVoiceProjection } from '@/store/quick-entry'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'

import { useComposerVoice } from './use-composer-voice'

const conversationEnd = vi.fn(async () => undefined)
let conversationStatus: 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking' = 'listening'

let conversationOptions: {
  onFatalError?: () => void
  onSubmit: (text: string) => Promise<void> | void
} | null = null

vi.mock('./use-voice-conversation', () => ({
  useVoiceConversation: (options: typeof conversationOptions) => {
    conversationOptions = options

    return { end: conversationEnd, status: conversationStatus }
  }
}))

vi.mock('./use-voice-recorder', () => ({
  useVoiceRecorder: () => ({ dictate: vi.fn(), voiceActivityState: 'idle', voiceStatus: 'idle' })
}))
vi.mock('./use-auto-speak-replies', () => ({ useAutoSpeakReplies: vi.fn() }))
vi.mock('../scope', () => ({ useComposerScope: () => ({ $messages: atom([]) }) }))
vi.mock('../focus', () => ({ onComposerVoiceToggleRequest: () => () => undefined }))
vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      assistant: { thread: { readAloudFailed: 'failed' } },
      notifications: { voice: { sayStopToEnd: () => 'stop' } },
      settings: { config: { autosaveFailed: 'failed' } }
    }
  })
}))
vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))
vi.mock('@/lib/wake-indicator', () => ({
  clearWakeIndicator: vi.fn(),
  syncWakeIndicatorWithVoice: vi.fn(() => false)
}))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))
vi.mock('@/store/wake-word', () => ({ resumeWakeAfterVoice: vi.fn() }))

function renderVoice(
  onSubmit = vi.fn(async () => true),
  options: { disabled?: boolean; target?: 'main' | `tile:${string}` } = {}
) {
  const settings = {
    disabled: options.disabled ?? false,
    target: options.target ?? ('main' as const)
  }

  return {
    hook: renderHook(() =>
      useComposerVoice({
        busy: false,
        clearDraft: vi.fn(),
        disabled: settings.disabled,
        focusInput: vi.fn(),
        insertText: vi.fn(),
        maxRecordingSeconds: 60,
        onSubmit,
        onTranscribeAudio: vi.fn(async () => 'hello'),
        sessionId: 'runtime-1',
        target: settings.target
      })
    ),
    onSubmit,
    settings
  }
}

const binding = { durableSessionId: 'stored-1', profile: 'default', runtimeSessionId: 'runtime-1' }

describe('useComposerVoice Quick Entry binding', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    conversationOptions = null
    conversationStatus = 'listening'
    setQuickEntryVoiceProjection({ active: false, available: false, error: null, status: 'idle' })
    $activeGatewayProfile.set('default')
    $activeSessionId.set('runtime-1')
    $selectedStoredSessionId.set('stored-1')
  })

  afterEach(cleanup)

  it('consumes a matching bound request and starts the existing main conversation controller', async () => {
    requestVoiceConversationStart(binding)
    const { hook } = renderVoice()

    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(true))
  })

  it('projects the active main conversation lifecycle for Quick Entry', async () => {
    requestVoiceConversationStart(binding)
    const { hook } = renderVoice()

    await waitFor(() =>
      expect($quickEntryVoiceProjection.get()).toEqual({
        active: true,
        available: true,
        error: null,
        status: 'listening'
      })
    )

    conversationStatus = 'thinking'
    hook.rerender()

    await waitFor(() => expect($quickEntryVoiceProjection.get().status).toBe('thinking'))
  })

  it.each([
    ['profile', () => $activeGatewayProfile.set('other')],
    ['runtime', () => $activeSessionId.set('runtime-2')],
    ['durable', () => $selectedStoredSessionId.set('stored-2')]
  ])('ends capture and blocks canonical submit after %s drift', async (_kind, drift) => {
    requestVoiceConversationStart(binding)
    const { hook, onSubmit } = renderVoice()
    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(true))

    act(() => drift())
    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(false))
    expect(conversationEnd).toHaveBeenCalled()
    expect($quickEntryVoiceProjection.get()).toEqual({
      active: false,
      available: true,
      error: 'failed',
      status: 'idle'
    })

    await act(async () => {
      await conversationOptions?.onSubmit('must not send')
    })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('projects fatal failure and clears it only on a valid fresh start', async () => {
    requestVoiceConversationStart(binding)
    const { hook } = renderVoice()
    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(true))

    act(() => conversationOptions?.onFatalError?.())
    await waitFor(() =>
      expect($quickEntryVoiceProjection.get()).toEqual({
        active: false,
        available: true,
        error: 'failed',
        status: 'idle'
      })
    )

    act(() => requestVoiceConversationStart(binding))
    await waitFor(() =>
      expect($quickEntryVoiceProjection.get()).toEqual({
        active: true,
        available: true,
        error: null,
        status: 'listening'
      })
    )

    act(() => conversationOptions?.onFatalError?.())
    await waitFor(() => expect($quickEntryVoiceProjection.get().error).toBe('failed'))
    act(() => hook.result.current.startConversation())
    await waitFor(() => expect($quickEntryVoiceProjection.get().error).toBeNull())
  })

  it('does not leave the projection active after explicit end or unmount', async () => {
    requestVoiceConversationStart(binding)
    const { hook } = renderVoice()
    await waitFor(() => expect($quickEntryVoiceProjection.get().active).toBe(true))

    act(() => hook.result.current.endConversation())
    await waitFor(() => expect($quickEntryVoiceProjection.get().active).toBe(false))

    act(() => requestVoiceConversationStart(binding))
    await waitFor(() => expect($quickEntryVoiceProjection.get().active).toBe(true))
    hook.unmount()

    expect($quickEntryVoiceProjection.get()).toEqual({
      active: false,
      available: false,
      error: null,
      status: 'idle'
    })
  })

  it('clears a Quick Entry binding before a later ordinary voice session starts', async () => {
    requestVoiceConversationStart(binding)
    const { hook, onSubmit } = renderVoice()
    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(true))

    act(() => hook.result.current.endConversation())
    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(false))
    act(() => {
      $activeSessionId.set('runtime-2')
      $selectedStoredSessionId.set('stored-2')
      hook.result.current.startConversation()
    })

    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(true))
    await act(async () => conversationOptions?.onSubmit('ordinary voice turn'))
    expect(onSubmit).toHaveBeenCalledWith('ordinary voice turn')
  })

  it('consumes a bound request received while voice is active instead of restarting it later', async () => {
    requestVoiceConversationStart(binding)
    const { hook } = renderVoice()
    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(true))

    act(() => {
      requestVoiceConversationStart({
        durableSessionId: 'stored-2',
        profile: 'default',
        runtimeSessionId: 'runtime-2'
      })
      hook.result.current.endConversation()
      $activeSessionId.set('runtime-2')
      $selectedStoredSessionId.set('stored-2')
    })

    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(false))
  })

  it('consumes a legacy unbound wake request received while active instead of restarting later', async () => {
    requestVoiceConversationStart(binding)
    const { hook } = renderVoice()
    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(true))

    act(() => {
      requestVoiceConversationStart()
      hook.result.current.endConversation()
    })

    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(false))
  })

  it('consumes a mismatched bound request while disabled before identity can drift to match', async () => {
    const { hook, settings } = renderVoice(undefined, { disabled: true })

    act(() =>
      requestVoiceConversationStart({
        durableSessionId: 'stored-2',
        profile: 'default',
        runtimeSessionId: 'runtime-2'
      })
    )
    act(() => {
      $activeSessionId.set('runtime-2')
      $selectedStoredSessionId.set('stored-2')
      settings.disabled = false
      hook.rerender()
    })

    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(false))
  })

  it('preserves a legacy unbound wake request while disabled until the main composer can consume it', async () => {
    const { hook, settings } = renderVoice(undefined, { disabled: true })

    act(() => requestVoiceConversationStart())
    expect(hook.result.current.voiceConversationActive).toBe(false)

    act(() => {
      settings.disabled = false
      hook.rerender()
    })

    await waitFor(() => expect(hook.result.current.voiceConversationActive).toBe(true))
  })

  it('never lets a tile composer publish the global projection', () => {
    const authoritative = { active: true, available: true, error: null, status: 'speaking' } as const
    setQuickEntryVoiceProjection(authoritative)

    const { hook } = renderVoice(undefined, { target: 'tile:one' })
    hook.rerender()
    hook.unmount()

    expect($quickEntryVoiceProjection.get()).toEqual(authoritative)
  })
})
