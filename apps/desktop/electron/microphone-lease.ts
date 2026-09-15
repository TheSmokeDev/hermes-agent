export function createMicrophoneArbiter() {
  let lease: { senderId: number; token: string } | null = null

  return {
    acquire(senderId: number, token: string): boolean {
      if (!token || lease) {
        return false
      }

      lease = { senderId, token }

      return true
    },
    release(senderId: number, token: string): void {
      if (lease?.senderId === senderId && lease.token === token) {
        lease = null
      }
    },
    releaseWindow(senderId: number): void {
      if (lease?.senderId === senderId) {
        lease = null
      }
    }
  }
}
