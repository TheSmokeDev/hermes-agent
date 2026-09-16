import { acquireMicrophoneLease } from '@/app/chat/composer/hooks/composer-microphone-lease'
import { requestGatewayForAgent } from '@/store/gateway'
import type { PluginVoiceController } from '@/store/plugin-voice'

import {
  parsePluginVoiceOwner,
  type PluginVoiceDescriptor,
  type PluginVoiceOwner
} from '../../../electron/plugin-voice-contract'

interface PluginVoiceControllerDeps {
  prepare(owner: PluginVoiceOwner, signal: AbortSignal): Promise<{ session_id: string; stored_session_id: string }>
  pause(owner: PluginVoiceOwner): Promise<void>
  resume(owner: PluginVoiceOwner): Promise<void>
  close(id: string): void
}

const scopedWake = async (owner: PluginVoiceOwner, method: string): Promise<void> => {
  try {
    await requestGatewayForAgent(owner.connectionId, owner.profile, method, {})
  } catch (error) {
    // Older gateways may not expose wake detection; routing/auth failures still fail closed.
    if (!(error instanceof Error) || !('code' in error) || error.code !== -32601) {
      throw error
    }
  }
}

const nativeDeps: PluginVoiceControllerDeps = {
  prepare: async (owner, signal) => {
    const request = <T>(method: string, params: Record<string, unknown>) =>
      requestGatewayForAgent<T>(owner.connectionId, owner.profile, method, params, 30_000, signal)

    const prepared = await request<{ session_id: string; stored_session_id: string }>('session.prepare', {
      session_id: owner.sessionId,
      stored_session_id: owner.storedSessionId
    })

    if (prepared.session_id !== owner.sessionId || prepared.stored_session_id !== owner.storedSessionId) {
      return prepared
    }

    // Live activation adds a viewer without displacing the main window's subscription.
    // Its reply is the live session payload (no stored_session_id); the prepared ids are the owner.
    await request('session.activate', { session_id: owner.sessionId, omit_messages: true })

    return prepared
  },
  pause: owner => scopedWake(owner, 'wake.pause'),
  resume: owner => scopedWake(owner, 'wake.resume'),
  close: id => window.hermesDesktop?.hud?.voice?.stop(id)
}

export function createPluginVoiceController(descriptor: PluginVoiceDescriptor, deps = nativeDeps) {
  const owner = parsePluginVoiceOwner(descriptor.owner)
  const lifetime = new AbortController()
  let microphonePermission = new AbortController()
  const token = Symbol('plugin-voice-hud')
  let preparation: Promise<PluginVoiceOwner> | null = null

  const stop = () => {
    if (!lifetime.signal.aborted) {
      lifetime.abort()
      deps.close(descriptor.id)
    }
  }

  const prepareSession = (): Promise<PluginVoiceOwner> => {
    if (lifetime.signal.aborted) {
      return Promise.reject(new Error('Voice owner is no longer available'))
    }

    if (preparation) {
      return preparation
    }

    preparation = deps
      .prepare(owner, lifetime.signal)
      .then(result => {
        if (
          lifetime.signal.aborted ||
          result.session_id !== owner.sessionId ||
          result.stored_session_id !== owner.storedSessionId
        ) {
          throw new Error('Voice owner no longer matches the prepared conversation')
        }

        return owner
      })
      .catch(error => {
        stop()
        throw error
      })
      .finally(() => {
        preparation = null
      })

    return preparation
  }

  const controller: PluginVoiceController = {
    capabilities: { microphoneLease: 1, pinnedRest: 1, prepareSession: 1 },
    owner,
    signal: lifetime.signal,
    prepareSession,
    acquire: options =>
      acquireMicrophoneLease({
        owner: token,
        ownerSignal: lifetime.signal,
        signal: options?.signal
          ? AbortSignal.any([options.signal, microphonePermission.signal])
          : microphonePermission.signal,
        voiceContextIsCurrent: () => !lifetime.signal.aborted,
        pause: async () => {
          await prepareSession()
          await deps.pause(owner)
        },
        resume: () => deps.resume(owner)
      }),
    stop
  }

  return {
    controller,
    dispose: () => lifetime.abort(),
    setMicrophoneAllowed(allowed: boolean) {
      if (!allowed) {
        microphonePermission.abort()
      } else if (microphonePermission.signal.aborted) {
        microphonePermission = new AbortController()
      }
    }
  }
}
