import { useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'

import type { VoiceActivityState, VoiceStatus } from '../types'

import type { ComposerVoiceLease } from './composer-microphone-lease'
import { useMicRecorder } from './use-mic-recorder'

interface VoiceRecorderOptions {
  acquire?: (options?: { signal?: AbortSignal }) => Promise<ComposerVoiceLease | null>
  signal?: AbortSignal
  maxRecordingSeconds: number
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  focusInput: () => void
  onTranscript: (text: string) => void
}

export function useVoiceRecorder({
  acquire,
  signal,
  maxRecordingSeconds,
  onTranscribeAudio,
  focusInput,
  onTranscript
}: VoiceRecorderOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const { handle, level, recording } = useMicRecorder(voiceCopy)
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('idle')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const startedAtRef = useRef(0)
  const leaseRef = useRef<ComposerVoiceLease | null>(null)
  const attemptRef = useRef<AbortController | null>(null)
  const intervalRef = useRef<number | null>(null)
  const timeoutRef = useRef<number | null>(null)

  const clearTimers = () => {
    if (intervalRef.current) {
      window.clearInterval(intervalRef.current)
      intervalRef.current = null
    }

    if (timeoutRef.current) {
      window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
  }

  const cancel = () => {
    attemptRef.current?.abort()
    attemptRef.current = null
    clearTimers()
    handle.cancel()
    leaseRef.current?.release()
    leaseRef.current = null
    setVoiceStatus('idle')
  }

  useEffect(() => {
    signal?.addEventListener('abort', cancel, { once: true })

    return () => {
      signal?.removeEventListener('abort', cancel)
      cancel()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- cleanup closes stable recorder/timer/lease refs for this lifetime
  }, [signal])

  const stop = async () => {
    clearTimers()
    const result = await handle.stop()
    leaseRef.current?.release()
    leaseRef.current = null

    if (signal?.aborted) {
      return
    }

    if (!result) {
      setVoiceStatus('idle')

      return
    }

    if (!onTranscribeAudio) {
      setVoiceStatus('idle')

      return
    }

    setVoiceStatus('transcribing')

    try {
      const transcript = (await onTranscribeAudio(result.audio)).trim()

      if (signal?.aborted) {
        return
      }

      if (!transcript) {
        notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
      } else {
        onTranscript(transcript)
      }
    } catch (error) {
      notifyError(error, voiceCopy.transcriptionFailed)
    } finally {
      setVoiceStatus('idle')

      if (!signal?.aborted) {
        focusInput()
      }
    }
  }

  const start = async () => {
    if (signal?.aborted || attemptRef.current) {
      return
    }

    if (!onTranscribeAudio) {
      notify({ kind: 'warning', title: voiceCopy.unavailable, message: voiceCopy.transcriptionUnavailable })

      return
    }

    const attempt = new AbortController()
    attemptRef.current = attempt
    const lease = acquire ? await acquire({ signal: attempt.signal }) : null

    if ((acquire && !lease) || attempt.signal.aborted || signal?.aborted) {
      lease?.release()

      if (attemptRef.current === attempt) {
        attemptRef.current = null
      }

      return
    }

    leaseRef.current = lease

    try {
      await handle.start({
        onError: error => {
          cancel()

          if (!signal?.aborted) {
            notifyError(error, voiceCopy.recordingFailed)
          }
        }
      })

      if (attempt.signal.aborted || signal?.aborted) {
        lease?.release()

        return
      }

      attemptRef.current = null
      startedAtRef.current = Date.now()
      setElapsedSeconds(0)
      setVoiceStatus('recording')
      intervalRef.current = window.setInterval(() => setElapsedSeconds((Date.now() - startedAtRef.current) / 1000), 250)
      const cap = Math.max(1, Math.min(Math.trunc(maxRecordingSeconds), 600))
      timeoutRef.current = window.setTimeout(() => void stop(), cap * 1000)
    } catch (error) {
      lease?.release()

      if (attemptRef.current !== attempt || attempt.signal.aborted || signal?.aborted) {
        return
      }

      attemptRef.current = null
      leaseRef.current = null
      setVoiceStatus('idle')

      if (!signal?.aborted) {
        notifyError(error, voiceCopy.recordingFailed)
      }
    }
  }

  const dictate = () => {
    if (attemptRef.current) {
      cancel()
    } else if (recording) {
      void stop()
    } else if (voiceStatus === 'idle') {
      void start()
    }
  }

  const voiceActivityState: VoiceActivityState = {
    elapsedSeconds,
    level,
    status: voiceStatus
  }

  return { dictate, voiceActivityState, voiceStatus }
}
