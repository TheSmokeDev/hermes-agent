import { describe, expect, it } from 'vitest'

import { notificationActionPayload } from './notification-action'

describe('notificationActionPayload', () => {
  it('echoes the exact notification requestId', () => {
    const requestId = 'approval-a'

    expect(notificationActionPayload({ requestId, sessionId: 'session-1' }, 'approve')).toEqual({
      actionId: 'approve',
      requestId,
      sessionId: 'session-1'
    })
  })

  it('keeps legacy metadata absent instead of inventing an id', () => {
    expect(notificationActionPayload({ sessionId: 'session-1' }, 'reject')).toEqual({
      actionId: 'reject',
      requestId: undefined,
      sessionId: 'session-1'
    })
  })
})
