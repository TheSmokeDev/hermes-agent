import { createMicrophoneArbiter } from './microphone-lease'

export interface MicrophoneWakeClient {
  pause(): Promise<void>
  resume(): Promise<void>
}

export function createMicrophoneWakeCoordinator() {
  const arbiter = createMicrophoneArbiter()
  const clients = new Map<number, MicrophoneWakeClient>()
  let active: {
    senderId: number
    token: string
    peers: MicrophoneWakeClient[]
    paused: Promise<void>
    released: boolean
  } | null = null

  const release = async (senderId: number, token: string) => {
    const lease = active

    if (!lease || lease.senderId !== senderId || lease.token !== token || lease.released) {
      return
    }

    lease.released = true
    await lease.paused.catch(() => undefined)
    await Promise.allSettled(lease.peers.map(peer => peer.resume()))
    arbiter.release(senderId, token)

    if (active === lease) {
      active = null
    }
  }

  return {
    register(senderId: number, client: MicrophoneWakeClient) {
      clients.set(senderId, client)
    },
    async acquire(senderId: number, token: string): Promise<boolean> {
      if (!arbiter.acquire(senderId, token)) {
        return false
      }

      const peers = [...clients].filter(([id]) => id !== senderId).map(([, client]) => client)
      const lease = { senderId, token, peers, released: false, paused: Promise.all(peers.map(peer => peer.pause())).then(() => undefined) }
      active = lease

      try {
        await lease.paused

        return !lease.released
      } catch {
        await release(senderId, token)

        return false
      }
    },
    release,
    async remove(senderId: number) {
      clients.delete(senderId)

      if (active?.senderId === senderId) {
        await release(senderId, active.token)
      }
    }
  }
}
