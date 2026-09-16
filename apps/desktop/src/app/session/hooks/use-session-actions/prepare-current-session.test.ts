import { describe, expect, it, vi } from 'vitest'

vi.mock('./utils', () => ({ resolveSessionProfile: vi.fn(async () => 'coder') }))

import { prepareCurrentSession, type PrepareCurrentSessionDeps } from './prepare-current-session'

function fixture() {
  const current = {
    routeToken: '/stored-a',
    selectedStoredSessionId: 'stored-a' as string | null,
    activeRuntimeId: null as string | null
  }

  const createSession = vi.fn(async () => null as string | null)

  const requestGateway = vi.fn(async (method: string) =>
    method === 'session.resume'
      ? { session_id: 'runtime-a' }
      : { session_id: 'runtime-a', stored_session_id: 'stored-a' }
  )

  const deps: PrepareCurrentSessionDeps = {
    activeRuntimeId: null,
    createSession,
    current: () => current,
    getRuntimeIdForStoredSession: () => null,
    owner: { connectionId: 'original-host', profile: 'coder' },
    isCurrent: () => true,
    publish: vi.fn(),
    requestGateway: requestGateway as PrepareCurrentSessionDeps['requestGateway'],
    routeToken: current.routeToken,
    routedStoredSessionId: 'stored-a',
    selectedStoredSessionId: 'stored-a'
  }

  return { createSession, current, deps, requestGateway }
}

describe('preparing the displayed conversation', () => {
  it('resumes and persists the original conversation without a prompt or replacement session', async () => {
    const { createSession, deps, requestGateway } = fixture()
    await expect(prepareCurrentSession(deps)).resolves.toBe('runtime-a')
    expect(requestGateway.mock.calls.map(([method]) => method)).toEqual(['session.resume', 'session.prepare'])
    expect(requestGateway).toHaveBeenLastCalledWith('session.prepare', {
      session_id: 'runtime-a',
      stored_session_id: 'stored-a'
    })
    expect(createSession).not.toHaveBeenCalled()
    expect(deps.publish).toHaveBeenCalledWith('runtime-a', 'stored-a', deps.owner)
  })

  it('does not publish a late persistence receipt after the user switches conversations', async () => {
    const { current, deps, requestGateway } = fixture()
    requestGateway.mockImplementation(async method => {
      if (method === 'session.prepare') {
        current.selectedStoredSessionId = 'stored-b'
        current.routeToken = '/stored-b'
      }

      return { session_id: 'runtime-a', stored_session_id: 'stored-a' }
    })
    await expect(prepareCurrentSession(deps)).rejects.toThrow('selected conversation changed')
    expect(deps.publish).not.toHaveBeenCalled()
  })

  it('never creates a replacement when an existing conversation cannot resume', async () => {
    const { createSession, deps, requestGateway } = fixture()
    requestGateway.mockRejectedValue(new Error('connection unavailable'))
    await expect(prepareCurrentSession(deps)).rejects.toThrow('could not be prepared')
    expect(createSession).not.toHaveBeenCalled()
    expect(deps.publish).not.toHaveBeenCalled()
  })

  it('adopts a newly created draft only after persistence confirms its exact identity', async () => {
    const { current, deps, createSession } = fixture()
    deps.routedStoredSessionId = null
    deps.selectedStoredSessionId = null
    deps.routeToken = current.routeToken = '/'
    current.selectedStoredSessionId = null
    createSession.mockImplementation(async () => {
      current.activeRuntimeId = 'runtime-a'
      current.selectedStoredSessionId = 'stored-a'
      current.routeToken = '/stored-a'

      return 'runtime-a'
    })
    await expect(prepareCurrentSession(deps)).resolves.toBe('runtime-a')
    expect(createSession).toHaveBeenCalledOnce()
    expect(deps.publish).toHaveBeenCalledWith('runtime-a', 'stored-a', deps.owner)
  })
})
