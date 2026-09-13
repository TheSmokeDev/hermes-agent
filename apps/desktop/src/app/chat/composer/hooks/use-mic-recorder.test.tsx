import { act, renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { useMicRecorder } from './use-mic-recorder'

const copy = {
  microphoneAccessDenied: 'denied',
  microphoneConstraintsUnsupported: 'constraints',
  microphoneInUse: 'busy',
  microphonePermissionDenied: 'denied',
  microphoneStartFailed: 'failed',
  microphoneUnsupported: 'unsupported',
  noMicrophone: 'missing'
}

afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(window, 'hermesDesktop')
})

it('closes a late microphone grant after cancellation or unmount without constructing a recorder', async () => {
  for (const unmount of [false, true]) {
    let grant!: (stream: MediaStream) => void
    const track = { stop: vi.fn() }
    const getUserMedia = vi.fn(
      () =>
        new Promise<MediaStream>(resolve => {
          grant = resolve
        })
    )
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })
    const recorder = Object.assign(vi.fn(), { isTypeSupported: () => true })
    vi.stubGlobal('MediaRecorder', recorder)
    const hook = renderHook(() => useMicRecorder(copy))
    let pending!: Promise<void>
    await act(async () => {
      pending = hook.result.current.handle.start()
      await Promise.resolve()
    })
    expect(getUserMedia).toHaveBeenCalledOnce()

    if (unmount) {
      hook.unmount()
    } else {
      act(() => hook.result.current.handle.cancel())
    }

    await act(async () => {
      grant({ getTracks: () => [track] } as unknown as MediaStream)
      await pending
    })
    expect(track.stop).toHaveBeenCalledOnce()
    expect(recorder).not.toHaveBeenCalled()

    if (!unmount) {
      hook.unmount()
    }
  }
})
