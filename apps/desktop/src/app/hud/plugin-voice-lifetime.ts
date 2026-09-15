import type { PluginVoiceDescriptor } from '../../../electron/plugin-voice-contract'

import type { createPluginVoiceController } from './plugin-voice-controller'

export function watchPluginVoiceLifetime(
  descriptor: PluginVoiceDescriptor,
  resource: ReturnType<typeof createPluginVoiceController>
): () => void {
  const { controller } = resource
  const stop = () => controller.stop()

  const offStopped = window.hermesDesktop?.hud?.voice?.onStopped(id => {
    if (id === descriptor.id) {
      resource.dispose()
    }
  })

  const offConnection = window.hermesDesktop?.connections?.onChanged?.(({ connectionId }) => {
    if (connectionId === controller.owner.connectionId) {
      stop()
    }
  })

  // Owner authorization must expire even while the control is collapsed or another app is focused.
  const timer = window.setInterval(() => {
    void controller.prepareSession().catch(stop)
  }, 10_000)

  window.addEventListener('beforeunload', resource.dispose)

  let disposed = false
  let permission: PermissionStatus | undefined

  const permissionChanged = () => {
    if (permission) {
      resource.setMicrophoneAllowed(permission.state !== 'denied')
    }
  }

  if (navigator.permissions?.query) {
    // Chromium supports microphone; TypeScript's cross-browser PermissionName omits it.
    void navigator.permissions.query({ name: 'microphone' as PermissionName }).then(status => {
      if (!disposed) {
        permission = status
        status.addEventListener('change', permissionChanged)
        permissionChanged()
      }
    }).catch(() => undefined)
  }

  return () => {
    disposed = true
    window.clearInterval(timer)
    window.removeEventListener('beforeunload', resource.dispose)
    offStopped?.()
    offConnection?.()
    permission?.removeEventListener('change', permissionChanged)
    resource.dispose()
  }
}
