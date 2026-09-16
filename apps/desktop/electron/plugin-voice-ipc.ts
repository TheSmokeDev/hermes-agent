import { BrowserWindow, ipcMain } from 'electron'

import type { createPluginVoiceHud } from './plugin-voice-hud'

export function registerPluginVoiceIpc(voice: ReturnType<typeof createPluginVoiceHud>) {
  ipcMain.handle('hermes:hud:voice:open', (event, request) => {
    if (!BrowserWindow.fromWebContents(event.sender)) {
      throw new Error('Voice requires an app window')
    }

    return voice.open(request)
  })
  ipcMain.handle('hermes:hud:voice:get', event => voice.descriptor(event.sender.id))
  ipcMain.on('hermes:hud:voice:unregister', (event, pluginId: unknown) => {
    const current = voice.current()

    if (BrowserWindow.fromWebContents(event.sender) && current?.pluginId === pluginId) {
      voice.stop(current.id)
    }
  })
  ipcMain.on('hermes:hud:voice:stop', (event, id) => {
    if (voice.descriptor(event.sender.id)?.id === id) {
      voice.stop(id)
    }
  })
}
