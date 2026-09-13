import http from 'node:http'
import type { AddressInfo } from 'node:net'

import { describe, expect, it } from 'vitest'

import { createDesktopTalkAuth } from './desktop-talk-auth'

describe('Desktop Talk native auth', () => {
  it('authenticates the owned local child without exposing its credential in reflected failures', async () => {
    const auth = createDesktopTalkAuth()
    const owner = auth.issue()
    const token = owner.env.HERMES_DESKTOP_TALK_TOKEN

    const server = http.createServer((request, response) => {
      expect(request.headers['x-hermes-session-token']).toBe('gateway-fixture')
      expect(request.headers['x-hermes-desktop-talk-token']).toBe(token)
      response.writeHead(403, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ detail: `Rejected ${token}` }))
    })

    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))

    const descriptor = {
      baseUrl: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
      mode: 'local',
      source: 'local',
      authMode: 'token',
      token: 'gateway-fixture'
    }

    owner.bind(descriptor.baseUrl, descriptor.token, () => true)

    try {
      await expect(
        auth.dispatch(descriptor, '/api/plugins/hermes-talk/status', async headers => {
          const response = await fetch(`${descriptor.baseUrl}/api/plugins/hermes-talk/status`, {
            headers: { 'X-Hermes-Session-Token': descriptor.token, ...headers }
          })

          throw new Error(`${response.status}: ${await response.text()}`)
        })
      ).rejects.toThrow('403: {"detail":"Rejected [redacted]"}')
      expect(JSON.stringify(descriptor)).not.toContain(token)
    } finally {
      owner.revoke()
      server.closeAllConnections()
      await new Promise<void>((resolve, reject) => server.close(error => (error ? reject(error) : resolve())))
    }
  })

  it('keeps credentials out of remote and escaped routes and retires replaced or exited owners', async () => {
    const auth = createDesktopTalkAuth()
    const first = auth.issue()

    const descriptor = {
      baseUrl: 'http://127.0.0.1:12345',
      mode: 'local',
      source: 'local',
      authMode: 'token',
      token: 'gateway-first'
    }

    const path = '/api/plugins/hermes-talk/status'
    const headers = (value: Record<string, string>) => Promise.resolve(value)
    let current = true
    first.bind(descriptor.baseUrl, descriptor.token, () => current)

    for (const source of ['ssh', 'url', 'cloud']) {
      expect(await auth.dispatch({ ...descriptor, mode: 'remote', source }, path, headers)).toEqual({})
    }

    for (const route of [
      '/api/status',
      '/api/plugins/other/status',
      '/api/plugins/hermes-talkish/status',
      '/api/plugins/hermes-talk/../other',
      '/api/plugins/hermes-talk/%2e%2e/other',
      '/api/plugins/hermes-talk\\..\\other'
    ]) {
      expect(await auth.dispatch(descriptor, route, headers)).toEqual({})
    }

    expect(await auth.dispatch({ ...descriptor, token: 'different' }, path, headers)).toEqual({})
    expect(await auth.dispatch(descriptor, `${path}?profile=research`, headers)).toEqual({
      'X-Hermes-Desktop-Talk-Token': first.env.HERMES_DESKTOP_TALK_TOKEN
    })
    current = false
    expect(await auth.dispatch(descriptor, path, headers)).toEqual({})

    const replacement = auth.issue()
    replacement.bind(descriptor.baseUrl, descriptor.token, () => true)
    first.revoke()
    expect(await auth.dispatch(descriptor, path, headers)).toEqual({
      'X-Hermes-Desktop-Talk-Token': replacement.env.HERMES_DESKTOP_TALK_TOKEN
    })
    replacement.revoke()
    expect(await auth.dispatch(descriptor, path, headers)).toEqual({})
    replacement.bind(descriptor.baseUrl, descriptor.token, () => true)
    expect(await auth.dispatch(descriptor, path, headers)).toEqual({})
  })
})
