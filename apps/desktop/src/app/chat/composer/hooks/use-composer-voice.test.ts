import { describe, expect, it, vi } from 'vitest'

import { acquireMicrophoneLease, disposeAssistantSubscriptions, runVoiceControllerCallback } from './use-composer-voice'

describe('composer voice ownership', () => {
  it('keeps ownership and waits for pause settlement when aborted', async () => {
    let settlePause!: () => void
    let pauseSettled = false

    const pause = () =>
      new Promise<void>(resolve => {
        settlePause = () => {
          pauseSettled = true
          resolve()
        }
      })

    const resume = vi.fn(() => {
      expect(pauseSettled).toBe(true)
    })

    const controller = new AbortController()

    const acquired = acquireMicrophoneLease({
      owner: Symbol('owner'),
      pause,
      resume,
      signal: controller.signal,
      voiceContextIsCurrent: () => true
    })

    controller.abort()
    await Promise.resolve()
    expect(resume).not.toHaveBeenCalled()

    settlePause()
    await expect(acquired).resolves.toBeNull()
    expect(resume).toHaveBeenCalledOnce()
  })

  it('releases once and rearms only after the ownership barrier', async () => {
    let settlePause!: () => void
    let pauseSettled = false

    const pause = () =>
      new Promise<void>(resolve => {
        settlePause = () => {
          pauseSettled = true
          resolve()
        }
      })

    const resume = vi.fn(() => expect(pauseSettled).toBe(true))

    const acquired = acquireMicrophoneLease({
      owner: Symbol('owner'),
      pause,
      resume,
      voiceContextIsCurrent: () => true
    })

    settlePause()
    const lease = await acquired
    lease?.release()
    lease?.release()

    expect(resume).toHaveBeenCalledOnce()
  })

  it('checks context before and after a delayed submit on session switch', async () => {
    let current = true
    let settleSubmit!: () => void
    const isCurrent = vi.fn(() => current)

    const submit = () =>
      new Promise<void>(resolve => {
        settleSubmit = resolve
      })

    const completion = runVoiceControllerCallback(isCurrent, submit)

    current = false
    settleSubmit()
    await completion

    expect(isCurrent).toHaveBeenCalledTimes(2)
  })

  it('proactively disposes assistant subscriptions on context replacement', () => {
    const first = vi.fn()
    const second = vi.fn()
    const disposers = new Set([first, second])

    disposeAssistantSubscriptions(disposers)

    expect(first).toHaveBeenCalledOnce()
    expect(second).toHaveBeenCalledOnce()
    expect(disposers.size).toBe(0)
  })
})

describe('exclusive capture lifetimes', () => {
  it('blocks every competing start through pause and abort, and an old release cannot free its successor', async () => {
    let settle!: () => void
    const ownerAbort = new AbortController()

    const waiting = acquireMicrophoneLease({
      owner: Symbol('native'),
      voiceContextIsCurrent: () => true,
      pause: () =>
        new Promise<void>(resolve => {
          settle = resolve
        }),
      resume: () => undefined,
      ownerSignal: ownerAbort.signal
    })

    const competitor = () =>
      acquireMicrophoneLease({
        owner: Symbol('plugin'),
        voiceContextIsCurrent: () => true,
        pause: async () => {},
        resume: () => {}
      })

    expect(await competitor()).toBeNull()
    ownerAbort.abort()
    expect(await competitor()).toBeNull()
    settle()
    expect(await waiting).toBeNull()
    const first = await competitor()
    expect(first).not.toBeNull()
    first!.release()
    expect(first!.signal.aborted).toBe(true)
    const second = await competitor()
    first!.release()
    expect(await competitor()).toBeNull()
    second!.release()
  })

  it('aborts the acquired lease on owner disposal and fences stale starts after a route switch', async () => {
    const owner = new AbortController()
    let current = true
    const pause = vi.fn(async () => {})
    const resume = vi.fn()

    const acquire = () =>
      acquireMicrophoneLease({
        owner: Symbol('composer'),
        ownerSignal: owner.signal,
        voiceContextIsCurrent: () => current,
        pause,
        resume
      })

    const lease = await acquire()
    const close = vi.fn()
    lease!.signal.addEventListener('abort', close)
    current = false
    owner.abort()
    expect(close).toHaveBeenCalledOnce()
    expect(resume).toHaveBeenCalledOnce()
    expect(await acquire()).toBeNull()
    expect(pause).toHaveBeenCalledOnce()
    lease!.release()
    expect(resume).toHaveBeenCalledOnce()
  })
})
