import { computed } from 'nanostores'

import { $gateway } from '@/store/gateway'
import {
  $activeGatewayProfile,
  $newChatConnectionId,
  $newChatProfile,
  $newChatRoute,
  resolveNewChatOwnerRoute
} from '@/store/profile'
import { $connection, $selectedStoredSessionId, $sessions } from '@/store/session'
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

export interface ComposerVoiceTarget extends Pick<ComposerVoiceOwner, 'connectionId' | 'profile'> {
  sessionId: string | null
  storedSessionId: string | null
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

export function composerVoiceOwnerKey(sessionId: string | null | undefined, allowDraft = false) {
  return computed(
    [
      $sessionStates,
      $sessionTiles,
      $sessions,
      $selectedStoredSessionId,
      $gateway,
      $connection,
      $activeGatewayProfile,
      $newChatConnectionId,
      $newChatProfile,
      $newChatRoute
    ],
    () => {
      if (!sessionId && allowDraft) {
        const storedSessionId = $selectedStoredSessionId.get()
        const route = storedSessionId ? knownOwnerForSession(storedSessionId) : resolveNewChatOwnerRoute()

        return JSON.stringify(
          route && typeof route === 'object' && route.connectionId?.trim() && route.profile?.trim()
            ? { connectionId: route.connectionId, profile: route.profile, sessionId: null, storedSessionId }
            : null
        )
      }

      return JSON.stringify(composerVoiceOwner(sessionId))
    }
  )
}
