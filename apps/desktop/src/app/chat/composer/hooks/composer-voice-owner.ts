import { computed } from 'nanostores'

import { $sessions } from '@/store/session'
import {
  $sessionStates,
  $sessionTiles,
  knownOwnerForSession,
  storedSessionIdForRuntimeId
} from '@/store/session-states'

export interface ComposerVoiceOwner {
  connectionId: string
  profile: string
  sessionId: string
  storedSessionId: string
}

export function composerVoiceOwner(sessionId: string | null | undefined): ComposerVoiceOwner | null {
  if (!sessionId) {
    return null
  }

  const storedSessionId = storedSessionIdForRuntimeId(sessionId)
  const route = knownOwnerForSession(sessionId)

  if (
    !storedSessionId ||
    !route ||
    typeof route !== 'object' ||
    !route.connectionId?.trim() ||
    !route.profile?.trim()
  ) {
    return null
  }

  return { connectionId: route.connectionId, profile: route.profile, sessionId, storedSessionId }
}

export function composerVoiceOwnerKey(sessionId: string | null | undefined) {
  return computed([$sessionStates, $sessionTiles, $sessions], () => JSON.stringify(composerVoiceOwner(sessionId)))
}
