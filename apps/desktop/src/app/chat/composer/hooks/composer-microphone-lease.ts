export interface ComposerVoiceLease {
  signal: AbortSignal
  release: () => void
}

let microphoneOwner: symbol | null = null

export async function acquireMicrophoneLease({
  voiceContextIsCurrent,
  owner,
  pause,
  resume,
  signal,
  ownerSignal
}: {
  voiceContextIsCurrent: () => boolean
  owner: symbol
  pause: () => Promise<void>
  resume: () => Promise<void> | void
  signal?: AbortSignal
  ownerSignal?: AbortSignal
}): Promise<ComposerVoiceLease | null> {
  if (!voiceContextIsCurrent() || signal?.aborted || ownerSignal?.aborted || microphoneOwner !== null) {
    return null
  }

  // Every start, including native dictation/conversation, reserves before its
  // first await. A pending wake pause is still an exclusive capture attempt.
  const acquisition = Symbol(owner.description)
  microphoneOwner = acquisition
  const lifetime = new AbortController()
  const native = typeof window === 'undefined' ? undefined : window.hermesDesktop?.microphone
  const nativeToken = crypto.randomUUID()
  let nativeGranted = false
  let paused = false
  let pauseSettled = false
  let released = false

  const release = () => {
    lifetime.abort()

    if (!pauseSettled || released) {
      return
    }

    released = true
    signal?.removeEventListener('abort', release)
    ownerSignal?.removeEventListener('abort', release)

    const clear = () => {
      if (nativeGranted) {
        native?.release(nativeToken)
        nativeGranted = false
      }

      if (microphoneOwner === acquisition) {
        microphoneOwner = null
      }
    }

    // Wake re-arm must settle before a competing start can pause it again.
    try {
      const resumed = paused ? resume() : undefined

      if (resumed) {
        void resumed.then(clear, clear)
      } else {
        clear()
      }
    } catch {
      clear()
    }
  }

  signal?.addEventListener('abort', release, { once: true })
  ownerSignal?.addEventListener('abort', release, { once: true })

  try {
    if (native) {
      nativeGranted = await native.acquire(nativeToken)

      if (!nativeGranted) {
        lifetime.abort()
      }
    }

    if (!lifetime.signal.aborted) {
      paused = true
      await pause()
    }
  } catch {
    lifetime.abort()
  }

  pauseSettled = true

  if (lifetime.signal.aborted || !voiceContextIsCurrent() || microphoneOwner !== acquisition) {
    release()

    return null
  }

  return { signal: lifetime.signal, release }
}
