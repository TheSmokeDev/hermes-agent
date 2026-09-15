import { atom } from 'nanostores'
import type { ReactNode } from 'react'

import type { ComposerVoiceLease } from '@/app/chat/composer/hooks/composer-microphone-lease'

import { parsePluginVoiceOwner, type PluginVoiceOwner } from '../../electron/plugin-voice-contract'

export type { PluginVoiceOwner } from '../../electron/plugin-voice-contract'

export interface PluginVoiceController {
  capabilities: { microphoneLease: 1; pinnedRest: 1; prepareSession: 1 }
  readonly owner: Readonly<PluginVoiceOwner>
  readonly signal: AbortSignal
  prepareSession(): Promise<PluginVoiceOwner>
  acquire(options?: { signal?: AbortSignal }): Promise<ComposerVoiceLease | null>
  stop(): void
}

export interface PluginVoiceSurfaceProps {
  controller: PluginVoiceController
}

export interface PluginVoice {
  readonly available: boolean
  register(render: (props: PluginVoiceSurfaceProps) => ReactNode): () => void
  open(owner: PluginVoiceOwner): Promise<void>
}

export const $pluginVoiceRenderers = atom<ReadonlyMap<string, (props: PluginVoiceSurfaceProps) => ReactNode>>(new Map())

export function createPluginVoice(pluginId: string, track: (dispose: () => void) => () => void): PluginVoice {
  return {
    get available() {
      return Boolean(window.hermesDesktop?.hud?.voice && window.hermesDesktop?.microphone)
    },
    register: render => {
      if ($pluginVoiceRenderers.get().has(pluginId)) {
        throw new Error('A plugin may register only one voice renderer')
      }

      $pluginVoiceRenderers.set(new Map($pluginVoiceRenderers.get()).set(pluginId, render))

      return track(() => {
        const next = new Map($pluginVoiceRenderers.get())
        next.delete(pluginId)
        $pluginVoiceRenderers.set(next)
        window.hermesDesktop?.hud?.voice?.unregister(pluginId)
      })
    },
    async open(raw) {
      const api = window.hermesDesktop?.hud?.voice

      if (!api || !window.hermesDesktop?.microphone) {
        throw new Error('Update Hermes Desktop to use a persistent voice HUD')
      }

      if (!$pluginVoiceRenderers.get().has(pluginId)) {
        throw new Error('The plugin has not registered a voice renderer')
      }

      await api.open({ pluginId, owner: parsePluginVoiceOwner(raw) })
    }
  }
}

export function isPluginVoiceWindow(search = window.location.search): boolean {
  const query = new URLSearchParams(search)

  return query.get('win') === 'hud' && query.get('voice') === '1'
}
