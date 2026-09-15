import { randomUUID } from 'node:crypto'

import { BrowserWindow, ipcMain, type WebContents } from 'electron'

import { createMicrophoneWakeCoordinator } from './microphone-wake'

export function registerMicrophoneIpc() {
  const microphone = createMicrophoneWakeCoordinator()
  const watched = new Set<number>()
  const replies = new Map<string, { senderId: number; settle(ok: boolean): void }>()

  const wake = (sender: WebContents, action: 'pause' | 'resume'): Promise<void> => {
    if (sender.isDestroyed()) {
      return Promise.resolve()
    }

    return new Promise((resolve, reject) => {
      const id = randomUUID()
      const timer = setTimeout(() => settle(false), 35_000)
      const settle = (ok: boolean) => {
        clearTimeout(timer)
        replies.delete(id)
        if (ok) {
          resolve()
        } else {
          reject(new Error('Could not hand off the wake microphone'))
        }
      }
      replies.set(id, { senderId: sender.id, settle })
      sender.send('hermes:microphone:wake', { id, action })
    })
  }

  const watch = (sender: WebContents) => {
    if (watched.has(sender.id)) {
      return
    }

    watched.add(sender.id)
    const gone = () => {
      for (const reply of replies.values()) {
        if (reply.senderId === sender.id) {
          reply.settle(true)
        }
      }
      void microphone.remove(sender.id)
    }
    sender.on('render-process-gone', gone)
    sender.once('destroyed', () => {
      gone()
      watched.delete(sender.id)
    })
  }

  ipcMain.on('hermes:microphone:watch-wake', event => {
    if (BrowserWindow.fromWebContents(event.sender)) {
      watch(event.sender)
      microphone.register(event.sender.id, {
        pause: () => wake(event.sender, 'pause'),
        resume: () => wake(event.sender, 'resume')
      })
    }
  })
  ipcMain.on('hermes:microphone:wake-settled', (event, id, ok) => {
    const reply = replies.get(id)

    if (reply?.senderId === event.sender.id) {
      reply.settle(ok === true)
    }
  })
  ipcMain.handle('hermes:microphone:acquire', (event, token: unknown) => {
    if (typeof token !== 'string' || !BrowserWindow.fromWebContents(event.sender)) {
      return false
    }

    watch(event.sender)

    return microphone.acquire(event.sender.id, token)
  })
  ipcMain.on('hermes:microphone:release', (event, token: unknown) => {
    if (typeof token === 'string') {
      void microphone.release(event.sender.id, token)
    }
  })
}
