import { randomUUID } from 'node:crypto'

import {
  parsePluginVoiceRequest,
  type PluginVoiceDescriptor,
  type PluginVoiceOwner,
  pluginVoiceOwnerKey
} from './plugin-voice-contract'

interface VoiceHudWindow {
  isDestroyed(): boolean
  webContents: { id: number; send(channel: string, payload: unknown): void }
}

interface PluginVoiceHudDeps {
  getWindow(): VoiceHudWindow | null
  spawn(): void
  focus(): void
  close(): void
  validateOwner(owner: PluginVoiceOwner): Promise<void>
}

export function createPluginVoiceHud(deps: PluginVoiceHudDeps) {
  let current: PluginVoiceDescriptor | null = null
  let pending: Promise<void> | null = null
  let closing = false

  const matches = (request: PluginVoiceDescriptor | ReturnType<typeof parsePluginVoiceRequest>) =>
    current?.pluginId === request.pluginId && pluginVoiceOwnerKey(current.owner) === pluginVoiceOwnerKey(request.owner)

  return {
    current: () => current,
    async open(raw: unknown): Promise<void> {
      const request = parsePluginVoiceRequest(raw)

      if (closing || (current && !matches(request)) || (!current && deps.getWindow())) {
        throw new Error('Close the current HUD before opening another voice owner')
      }

      if (pending) {
        await pending
        deps.focus()

        return
      }

      if (current) {
        deps.focus()

        return
      }

      const descriptor = { ...request, id: randomUUID() }
      current = descriptor

      const opening = (async () => {
        await deps.validateOwner(descriptor.owner)

        if (current !== descriptor || closing) {
          throw new Error('Voice owner was invalidated while opening')
        }

        deps.spawn()
      })()

      pending = opening

      try {
        await opening
      } catch (error) {
        if (current === descriptor) {
          current = null
        }

        throw error
      } finally {
        if (pending === opening) {
          pending = null
        }
      }
    },
    descriptor(senderId: number): PluginVoiceDescriptor | null {
      const win = deps.getWindow()

      return !closing && win && !win.isDestroyed() && win.webContents.id === senderId ? current : null
    },
    stop(id: string): void {
      if (!current || current.id !== id || closing) {
        return
      }

      closing = true
      const win = deps.getWindow()

      if (win && !win.isDestroyed()) {
        win.webContents.send('hermes:hud:voice:stopped', current.id)
        deps.close()
      } else {
        current = null
        closing = false
      }
    },
    closed(): void {
      current = null
      closing = false
    }
  }
}
