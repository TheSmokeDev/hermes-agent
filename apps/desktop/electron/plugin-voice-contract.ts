export interface PluginVoiceOwner {
  connectionId: string
  profile: string
  sessionId: string
  storedSessionId: string
}

export interface PluginVoiceRequest {
  pluginId: string
  owner: PluginVoiceOwner
}

export interface PluginVoiceDescriptor extends PluginVoiceRequest {
  id: string
}

export function parsePluginVoiceOwner(value: unknown): PluginVoiceOwner {
  if (!value || typeof value !== 'object') {
    throw new Error('Voice requires a prepared conversation owner')
  }

  const field = (key: keyof PluginVoiceOwner): string => {
    const result = key in value ? Reflect.get(value, key) : undefined

    if (typeof result !== 'string' || !result.trim() || result !== result.trim()) {
      throw new Error(`Voice owner requires ${key}`)
    }

    return result
  }

  return Object.freeze({
    connectionId: field('connectionId'),
    profile: field('profile'),
    sessionId: field('sessionId'),
    storedSessionId: field('storedSessionId')
  })
}

export function pluginVoiceOwnerKey(owner: PluginVoiceOwner): string {
  return JSON.stringify([owner.connectionId, owner.profile, owner.sessionId, owner.storedSessionId])
}

export function parsePluginVoiceRequest(value: unknown): PluginVoiceRequest {
  if (!value || typeof value !== 'object' || !('pluginId' in value) || !('owner' in value)) {
    throw new Error('Invalid plugin voice request')
  }

  if (typeof value.pluginId !== 'string' || !/^[a-zA-Z0-9_-]+$/.test(value.pluginId)) {
    throw new Error('Invalid voice plugin id')
  }

  return { pluginId: value.pluginId, owner: parsePluginVoiceOwner(value.owner) }
}
