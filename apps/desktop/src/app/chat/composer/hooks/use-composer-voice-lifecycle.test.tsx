import { act, renderHook, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const native = vi.hoisted(() => ({
  start: vi.fn(async () => {}),
  stop: vi.fn(async () => null),
  cancel: vi.fn(),
  end: vi.fn(async () => {}),
  enabled: false
}))

vi.mock('./use-mic-recorder', () => ({ useMicRecorder: () => ({ handle: native, level: 0, recording: false }) }))
vi.mock('./use-auto-speak-replies', () => ({ useAutoSpeakReplies: () => {} }))
vi.mock('./use-voice-conversation', () => ({
  useVoiceConversation: (options: { enabled: boolean }) => {
    native.enabled = options.enabled

    return { status: 'idle', end: native.end }
  }
}))
vi.mock('@/lib/tts-lease', () => ({
  CONVERSATION_LEASE: 'conversation',
  READ_ALOUD_LEASE: 'read',
  syncTtsLease: async () => {}
}))
vi.mock('@/store/wake-word', () => ({ resumeWakeAfterVoice: async () => {} }))

import { $sessions, _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { $newChatRoute } from '@/store/profile'
import { $sessionStates, $sessionTiles } from '@/store/session-states'

import { useComposerVoice } from './use-composer-voice'

function args(sessionId = 'runtime-one') {
  return {
    sessionId,
    target: 'main' as const,
    busy: false,
    disabled: false,
    clearDraft: vi.fn(),
    focusInput: vi.fn(),
    insertText: vi.fn(),
    maxRecordingSeconds: 60,
    onSubmit: vi.fn(async () => true),
    onTranscribeAudio: vi.fn(async () => 'hello')
  }
}

function bind() {
  $sessionTiles.set([
    {
      storedSessionId: 'stored-one',
      runtimeId: 'runtime-one',
      ownerRoute: { connectionId: 'original', profile: 'coder' }
    }
  ])
  setSessionOwnerHint('stored-one', { connectionId: 'original', profile: 'coder' })
}

afterEach(() => {
  $newChatRoute.set(null)
  $sessionTiles.set([])
  $sessionStates.set({})
  $sessions.set([])
  _resetSessionOwnerHintsForTests()
  vi.clearAllMocks()
})

describe('native and plugin composer capture', () => {
  it('coalesces draft preparation and requires the published controller for microphone ownership', async () => {
    $newChatRoute.set({ connectionId: 'original', profile: 'coder' })
    let finish!: (runtimeId: string) => void
    const onPrepareVoiceSession = vi.fn(
      () =>
        new Promise<string>(resolve => {
          finish = resolve
        })
    )
    const input = args()
    const hook = renderHook(
      ({ sessionId }: { sessionId: string | null }) =>
        useComposerVoice({
          ...input,
          sessionId,
          onPrepareVoiceSession
        }),
      { initialProps: { sessionId: null as string | null } }
    )
    const oldController = hook.result.current.voiceController
    expect(oldController.owner).toEqual({
      connectionId: 'original',
      profile: 'coder',
      sessionId: null,
      storedSessionId: null
    })
    const first = oldController.prepareSession()
    expect(oldController.prepareSession()).toBe(first)
    expect(onPrepareVoiceSession).toHaveBeenCalledOnce()
    await act(async () => {
      bind()
      hook.rerender({ sessionId: 'runtime-one' })
      finish('runtime-one')
      await first
    })
    await expect(first).resolves.toEqual(hook.result.current.voiceController.owner)
    expect(await oldController.acquire()).toBeNull()
    expect(input.onSubmit).not.toHaveBeenCalled()
    expect(native.start).not.toHaveBeenCalled()
    hook.unmount()
  })

  it('makes plugin capture and both native starts mutually exclusive', async () => {
    bind()
    const hook = renderHook(() => useComposerVoice(args()))
    let lease: Awaited<ReturnType<typeof hook.result.current.voiceController.acquire>>
    await act(async () => {
      lease = await hook.result.current.voiceController.acquire()
    })
    await act(async () => {
      await hook.result.current.startConversation()
      hook.result.current.dictate()
    })
    expect(native.enabled).toBe(false)
    expect(native.start).not.toHaveBeenCalled()
    await act(async () => {
      lease!.release()
    })
    await act(async () => {
      await hook.result.current.startConversation()
    })
    expect(native.enabled).toBe(true)
    expect(await hook.result.current.voiceController.acquire()).toBeNull()
    await act(async () => {
      hook.result.current.endConversation()
    })
    await act(async () => {
      hook.result.current.dictate()
    })
    await waitFor(() => expect(native.start).toHaveBeenCalledOnce())
    expect(await hook.result.current.voiceController.acquire()).toBeNull()
    hook.unmount()
  })

  it('pins the tile owner and aborts active or pending consumers on replacement and unmount', async () => {
    bind()
    const hook = renderHook(({ sessionId }) => useComposerVoice(args(sessionId)), {
      initialProps: { sessionId: 'runtime-one' },
      wrapper: StrictMode
    })
    expect(hook.result.current.voiceController.owner).toEqual({
      connectionId: 'original',
      profile: 'coder',
      sessionId: 'runtime-one',
      storedSessionId: 'stored-one'
    })
    const oldController = hook.result.current.voiceController
    const lease = await oldController.acquire()
    const close = vi.fn()
    lease!.signal.addEventListener('abort', close)
    await act(async () => {
      hook.rerender({ sessionId: 'runtime-two' })
    })
    expect(close).toHaveBeenCalledOnce()
    expect(await oldController.acquire()).toBeNull()
    const caller = new AbortController()
    const next = await hook.result.current.voiceController.acquire({ signal: caller.signal })
    expect(next).not.toBeNull()
    hook.unmount()
    expect(next!.signal.aborted).toBe(true)
  })
})
