import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { chatMessageText, collectUnspokenTurnSpeech } from '@/lib/chat-messages'
import { triggerHaptic } from '@/lib/haptics'
import { clearWakeIndicator, syncWakeIndicatorWithVoice } from '@/lib/wake-indicator'
import { $voiceConversationStartRequest, takeVoiceConversationStart, type VoiceSessionBinding } from '@/store/composer'
import { resetBrowseState } from '@/store/composer-input-history'
import { $gateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { setQuickEntryVoiceProjection } from '@/store/quick-entry'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'
import { $autoSpeakReplies, $voiceStopPhrase, setAutoSpeakReplies } from '@/store/voice-prefs'
import { resumeWakeAfterVoice } from '@/store/wake-word'

import type { ComposerTarget } from '../focus'
import { onComposerVoiceToggleRequest } from '../focus'
import { useComposerScope } from '../scope'
import type { ChatBarProps } from '../types'

import { useAutoSpeakReplies } from './use-auto-speak-replies'
import { useVoiceConversation } from './use-voice-conversation'
import { useVoiceRecorder } from './use-voice-recorder'

function voiceBindingMatches(expected: VoiceSessionBinding, actual: VoiceSessionBinding): boolean {
  return (
    expected.profile === actual.profile &&
    expected.runtimeSessionId === actual.runtimeSessionId &&
    expected.durableSessionId === actual.durableSessionId
  )
}

interface UseComposerVoiceArgs {
  busy: boolean
  clearDraft: () => void
  disabled: boolean
  focusInput: () => void
  insertText: (text: string) => void
  maxRecordingSeconds: number
  /** Interrupt the in-flight agent turn (Stop-button seam) — fired when the
   *  user speaks over the model while it is still generating. */
  onInterrupt?: () => Promise<void> | void
  onSubmit: ChatBarProps['onSubmit']
  onTranscribeAudio: ChatBarProps['onTranscribeAudio']
  sessionId: string | null | undefined
  /** This composer's focus-bus key — voice toggles targeting another
   *  composer (or the active one, when not us) are ignored. */
  target: ComposerTarget
}

/**
 * The composer's voice engine: push-to-talk dictation (transcript → draft), the
 * full voice-conversation loop, and auto-speak of replies. Self-contained — it
 * consumes the draft/submit primitives passed in but nothing depends back on it,
 * so it lifts cleanly out of ChatBar.
 */
export function useComposerVoice({
  busy,
  clearDraft,
  disabled,
  focusInput,
  insertText,
  maxRecordingSeconds,
  onInterrupt,
  onSubmit,
  onTranscribeAudio,
  sessionId,
  target
}: UseComposerVoiceArgs) {
  const { t } = useI18n()
  // A tile's composer speaks ITS transcript, not the primary chat's.
  const { $messages } = useComposerScope()
  const [voiceConversationActive, setVoiceConversationActive] = useState(false)
  const [voiceTerminalError, setVoiceTerminalError] = useState<null | 'failed'>(null)
  const lastSpokenIdRef = useRef<string | null>(null)
  const ownsWakeIndicatorRef = useRef(false)
  const boundVoiceSessionRef = useRef<VoiceSessionBinding | null>(null)
  const voiceStartRequest = useStore($voiceConversationStartRequest)
  const activeProfile = normalizeProfileKey(useStore($activeGatewayProfile))
  const activeRuntimeSessionId = useStore($activeSessionId)
  const activeDurableSessionId = useStore($selectedStoredSessionId)

  const activeBinding: VoiceSessionBinding | null = useMemo(
    () =>
      activeRuntimeSessionId
        ? {
            durableSessionId: activeDurableSessionId,
            profile: activeProfile,
            runtimeSessionId: activeRuntimeSessionId
          }
        : null,
    [activeDurableSessionId, activeProfile, activeRuntimeSessionId]
  )

  const voiceStartAdmissionRef = useRef({ activeBinding, disabled, target, voiceConversationActive })
  voiceStartAdmissionRef.current = { activeBinding, disabled, target, voiceConversationActive }

  const { dictate, voiceActivityState, voiceStatus } = useVoiceRecorder({
    focusInput,
    maxRecordingSeconds,
    onTranscript: insertText,
    onTranscribeAudio
  })

  /** Auto-speak selector: the latest unspoken reply only — a backlog collapses to the newest. */
  const pendingResponse = () => {
    const messages = $messages.get()
    const last = messages.findLast(m => m.role === 'assistant' && !m.hidden)

    if (!last || last.id === lastSpokenIdRef.current) {
      return null
    }

    const text = chatMessageText(last).trim()

    if (!text) {
      return null
    }

    return {
      id: last.id,
      pending: Boolean(last.pending),
      text
    }
  }

  /**
   * Voice-conversation selector: every unspoken assistant bubble of the turn,
   * in order — narration interims AND the final answer, not just whichever
   * bubble happens to be last. See `collectUnspokenTurnSpeech`.
   */
  const pendingTurnResponse = () => collectUnspokenTurnSpeech($messages.get(), lastSpokenIdRef.current)

  const consumePendingResponse = () => {
    const messages = $messages.get()
    const last = messages.findLast(m => m.role === 'assistant' && !m.hidden)

    if (last) {
      lastSpokenIdRef.current = last.id
    }
  }

  const submitVoiceTurn = async (text: string) => {
    const bound = boundVoiceSessionRef.current
    const runtimeSessionId = $activeSessionId.get()

    const currentBinding: VoiceSessionBinding | null = runtimeSessionId
      ? {
          durableSessionId: $selectedStoredSessionId.get(),
          profile: normalizeProfileKey($activeGatewayProfile.get()),
          runtimeSessionId
        }
      : null

    // Revalidate immediately before the canonical onSubmit seam. A transcript
    // captured for one identity must never land in another after drift.
    if (bound && (!currentBinding || !voiceBindingMatches(bound, currentBinding))) {
      setVoiceTerminalError('failed')
      setVoiceConversationActive(false)

      return
    }

    if (busy) {
      return
    }

    triggerHaptic('submit')
    resetBrowseState(sessionId)
    clearDraft()
    await onSubmit(text)
  }

  const wakePausedRef = useRef(false)
  // Resolves once the in-flight wake.pause round-trip completes (mic released by
  // the wake listener). The conversation awaits this before opening its own mic
  // so the two never contend for the device — on Windows especially, opening the
  // capture device while the wake listener still holds it makes getUserMedia
  // fail and the conversation never starts listening.
  const wakePauseBarrierRef = useRef<Promise<void> | null>(null)

  const conversation = useVoiceConversation({
    busy,
    consumePendingResponse,
    enabled: voiceConversationActive,
    onFatalError: () => {
      setVoiceTerminalError('failed')
      setVoiceConversationActive(false)
    },
    // Speaking over the model mid-generation interrupts the in-flight turn —
    // the same seam as the Stop button — so the interjection becomes the next
    // turn instead of waiting behind a reply the user already rejected.
    onInterrupt,
    // A spoken stop command ("stop", "never mind", "goodbye", …) ends the
    // hands-free conversation. Flipping the flag is the authoritative off
    // switch — the enabled=false prop + effect below drive conversation.end()
    // teardown (mic close, wake re-arm).
    onStopWord: () => {
      boundVoiceSessionRef.current = null
      setVoiceConversationActive(false)
    },
    onSubmit: submitVoiceTurn,
    onTranscribeAudio,
    pendingResponse: pendingTurnResponse,
    // Before the conversation opens the mic, wait for any in-flight wake.pause
    // to finish releasing the capture device (see wakePauseBarrierRef).
    beforeMicOpen: () => wakePauseBarrierRef.current ?? undefined
  })

  useEffect(() => {
    if (target !== 'main') {
      return
    }

    setQuickEntryVoiceProjection({
      active: voiceConversationActive,
      available: !disabled,
      error: voiceTerminalError,
      status: voiceConversationActive ? conversation.status : 'idle'
    })
  }, [conversation.status, disabled, target, voiceConversationActive, voiceTerminalError])

  useEffect(
    () => () => {
      if (target === 'main') {
        setQuickEntryVoiceProjection({ active: false, available: false, error: null, status: 'idle' })
      }
    },
    [target]
  )

  // eslint-disable-next-line no-restricted-syntax -- ownership token used only by unmount cleanup
  useEffect(() => {
    if (target !== 'main') {
      return
    }

    if (syncWakeIndicatorWithVoice(voiceConversationActive, conversation.status)) {
      ownsWakeIndicatorRef.current = voiceConversationActive
    }
  }, [conversation.status, target, voiceConversationActive])

  useEffect(
    () => () => {
      if (ownsWakeIndicatorRef.current) {
        clearWakeIndicator()
      }
    },
    []
  )

  // The `composer.voice` hotkey (Ctrl+B) toggles the conversation. Starting
  // with STT unconfigured lets the conversation surface its own "configure
  // speech-to-text" notice rather than silently no-opping.
  const toggleVoiceConversation = useCallback(() => {
    if (disabled) {
      return
    }

    if (voiceConversationActive) {
      boundVoiceSessionRef.current = null
      setVoiceConversationActive(false)
      void conversation.end()
    } else {
      boundVoiceSessionRef.current = null
      setVoiceTerminalError(null)
      setVoiceConversationActive(true)
    }
  }, [conversation, disabled, voiceConversationActive])

  useEffect(
    () => onComposerVoiceToggleRequest(toggled => toggled === target && toggleVoiceConversation()),
    [target, toggleVoiceConversation]
  )

  // Consume bound intents synchronously when they arrive while this controller
  // cannot admit a start. Waiting for the next React render leaves a race where
  // an end/identity change in the same batch can make a stale request valid.
  // Legacy unbound wake requests intentionally retain their remount latch.
  useEffect(
    () =>
      $voiceConversationStartRequest.listen(request => {
        const admission = voiceStartAdmissionRef.current

        if (
          admission.target === 'main' &&
          request &&
          (admission.voiceConversationActive || (admission.disabled && request.binding))
        ) {
          takeVoiceConversationStart(request, admission.activeBinding ?? undefined)
        }
      }),
    []
  )

  // eslint-disable-next-line no-restricted-syntax -- consumes a one-shot request into an identity guard, not mirrored reactive state
  useEffect(() => {
    if (target !== 'main' || !voiceStartRequest) {
      return
    }

    // Bound Quick Entry intents are attempts against one exact identity. Even
    // while disabled/already active they must be consumed now so they cannot
    // become valid after later identity or lifecycle drift. Legacy unbound wake
    // requests keep their historical latch-across-remount behavior.
    if (disabled || voiceConversationActive) {
      if (voiceConversationActive || voiceStartRequest.binding) {
        takeVoiceConversationStart(voiceStartRequest, activeBinding ?? undefined)
      }

      return
    }

    if (takeVoiceConversationStart(voiceStartRequest, activeBinding ?? undefined)) {
      boundVoiceSessionRef.current = voiceStartRequest?.binding ?? null
      setVoiceTerminalError(null)
      setVoiceConversationActive(true)
    }
  }, [activeBinding, disabled, target, voiceConversationActive, voiceStartRequest])

  useEffect(() => {
    const bound = boundVoiceSessionRef.current

    if (!voiceConversationActive || !bound) {
      return
    }

    if (!activeBinding || !voiceBindingMatches(bound, activeBinding)) {
      setVoiceTerminalError('failed')
      setVoiceConversationActive(false)
      void conversation.end()
    }
  }, [activeBinding, conversation, voiceConversationActive])

  const resumeWakeIfPaused = useCallback(() => {
    if (!wakePausedRef.current) {
      return
    }

    wakePausedRef.current = false
    wakePauseBarrierRef.current = null
    // Reconcile, don't just resume: the wake word is a persistent setting, so
    // ending a voice chat must re-arm the listener whenever config says
    // enabled — including when the raw resume loses the mic-release race.
    void resumeWakeAfterVoice()
  }, [])

  // The ref is a request token (did WE issue wake.pause?), not an atom mirror —
  // it guards resumeWakeIfPaused from resuming a detector another surface owns.
  const pauseWakeForVoice = useCallback(() => {
    wakePausedRef.current = true

    const barrier = (async () => {
      try {
        await $gateway.get()?.request('wake.pause', {})
      } catch {
        // No wake listener / older backend — nothing held the mic.
      }
    })()

    wakePauseBarrierRef.current = barrier

    return barrier
  }, [])

  useEffect(() => {
    if (voiceConversationActive) {
      pauseWakeForVoice()
    } else {
      resumeWakeIfPaused()
    }
  }, [pauseWakeForVoice, resumeWakeIfPaused, voiceConversationActive])

  // 'Say "stop" to end the voice chat.' notice when the conversation starts.
  // Phrase comes from voice.stop_phrases (first entry) so a custom phrase
  // renders correctly; a null phrase (stop_phrases: []) shows no notice.
  useEffect(() => {
    if (!voiceConversationActive) {
      return
    }

    const phrase = $voiceStopPhrase.get()

    if (phrase) {
      notify({
        id: 'voice-stop-hint',
        kind: 'info',
        icon: 'mic',
        message: t.notifications.voice.sayStopToEnd(phrase)
      })
    }
  }, [t, voiceConversationActive])

  useEffect(() => resumeWakeIfPaused, [resumeWakeIfPaused])

  // Explicit start/end for the on-screen conversation controls (the hotkey uses
  // the gated toggle above).
  const startConversation = useCallback(() => {
    boundVoiceSessionRef.current = null
    setVoiceTerminalError(null)
    setVoiceConversationActive(true)
  }, [])

  const endConversation = useCallback(() => {
    boundVoiceSessionRef.current = null
    setVoiceConversationActive(false)
    void conversation.end()
  }, [conversation])

  const handleToggleAutoSpeak = useCallback(() => {
    void setAutoSpeakReplies(!$autoSpeakReplies.get()).catch(error =>
      notifyError(error, t.settings.config.autosaveFailed)
    )
  }, [t])

  useAutoSpeakReplies({
    conversationActive: voiceConversationActive,
    failureLabel: t.assistant.thread.readAloudFailed,
    markSpoken: consumePendingResponse,
    pendingReply: pendingResponse,
    sessionId
  })

  return {
    conversation,
    dictate,
    endConversation,
    handleToggleAutoSpeak,
    startConversation,
    voiceActivityState,
    voiceConversationActive,
    voiceStatus
  }
}
