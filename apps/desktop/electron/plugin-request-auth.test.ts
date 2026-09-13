import http from 'node:http'
import type { AddressInfo } from 'node:net'

import { describe, expect, it, vi } from 'vitest'

import { pluginRequestHeaders, withPluginRequestAuth } from './plugin-request-auth'

describe('plugin request auth', () => {
  it('adds only the plugin gate alongside gateway auth and redacts a reflected error', async () => {
    const server = http.createServer((request, response) => {
      expect(request.headers['x-hermes-session-token']).toBe('gateway-fixture')
      expect(request.headers['x-hermes-plugin-token']).toBe('plugin-fixture')
      response.writeHead(403, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ detail: 'Rejected plugin-fixture' }))
    })

    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
    const request = { path: '/api/plugins/voice/session', pluginToken: 'plugin-fixture' }

    try {
      await expect(
        withPluginRequestAuth(request, async () => {
          const response = await fetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}${request.path}`, {
            headers: { 'X-Hermes-Session-Token': 'gateway-fixture', ...pluginRequestHeaders(request) }
          })

          throw new Error(`${response.status}: ${await response.text()}`)
        })
      ).rejects.toThrow('403: {"detail":"Rejected [redacted]"}')
    } finally {
      server.closeAllConnections()
      await new Promise<void>((resolve, reject) => server.close(error => (error ? reject(error) : resolve())))
    }
  })

  it('refuses non-plugin routes, encoded escapes, control characters and oversized gates before dispatch', async () => {
    const dispatch = vi.fn()

    for (const path of [
      '/api/status',
      '/api/plugins/voice/../other',
      '/api/plugins/voice/%2e%2e/status',
      '/api/plugins/voice\\..\\status'
    ]) {
      await expect(withPluginRequestAuth({ path, pluginToken: 'fixture' }, dispatch)).rejects.toThrow('plugin')
    }

    for (const pluginToken of ['', 'line\r\ninjection', 'x'.repeat(4097)]) {
      await expect(
        withPluginRequestAuth({ path: '/api/plugins/voice/session', pluginToken }, dispatch)
      ).rejects.toThrow('Invalid plugin token')
    }

    expect(dispatch).not.toHaveBeenCalled()
    expect(pluginRequestHeaders({ path: '/api/status' })).toEqual({})
  })
})
