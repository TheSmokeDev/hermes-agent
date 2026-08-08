import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { QuickEntryStatePush } from '@/store/quick-entry'

import { QuickEntryApp } from './quick-entry-app'

const dismiss = vi.fn()
const startVoice = vi.fn()
const submit = vi.fn()
let pushState: ((payload: QuickEntryStatePush) => void) | undefined

function installApi() {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      quickEntry: {
        dismiss,
        onShown: vi.fn(() => () => undefined),
        onState: vi.fn(callback => {
          pushState = callback

          return () => {
            pushState = undefined
          }
        }),
        startVoice,
        submit
      }
    }
  })
}

function state(overrides: Partial<QuickEntryStatePush> = {}): QuickEntryStatePush {
  return {
    connected: true,
    currentVoiceTargetAvailable: true,
    sessions: [{ id: 'stored-1', title: 'Stored chat' }],
    voice: { active: false, available: true, error: null, status: 'idle' },
    ...overrides
  }
}

describe('QuickEntryApp voice', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    pushState = undefined
    installApi()
  })

  afterEach(cleanup)

  it('starts current-session voice without submitting text or dismissing', () => {
    render(<QuickEntryApp />)
    act(() => pushState?.(state()))

    const mic = screen.getByRole('button', { name: 'Start voice conversation' }) as HTMLButtonElement
    expect(mic.disabled).toBe(false)
    fireEvent.click(mic)

    expect(startVoice).toHaveBeenCalledWith({ target: 'current' })
    expect(submit).not.toHaveBeenCalled()
    expect(dismiss).not.toHaveBeenCalled()
    expect(screen.queryByRole('textbox', { name: 'Quick Entry' })).not.toBeNull()
  })

  it('renders the projected lifecycle and a generic failure state', () => {
    render(<QuickEntryApp />)
    act(() => pushState?.(state({ voice: { active: true, available: true, error: null, status: 'listening' } })))
    expect(screen.getByRole('status').textContent).toBe('Listening')

    act(() => pushState?.(state({ voice: { active: false, available: true, error: 'failed', status: 'idle' } })))
    expect(screen.getByRole('status').textContent).toBe('Voice failed')

    act(() =>
      pushState?.({
        ...state(),
        voice: { active: false, available: true, error: null, status: 'unknown' }
      } as unknown as QuickEntryStatePush)
    )
    expect(screen.getByRole('status').textContent).toBe('Voice ready')
  })

  it('enables the mic only for an available inactive current-session target', () => {
    render(<QuickEntryApp />)
    const mic = screen.getByRole('button', { name: 'Start voice conversation' }) as HTMLButtonElement

    for (const unavailable of [
      state({ connected: false }),
      state({ currentVoiceTargetAvailable: false }),
      state({ voice: { active: false, available: false, error: null, status: 'idle' } }),
      state({ voice: { active: true, available: true, error: null, status: 'listening' } })
    ]) {
      act(() => pushState?.(unavailable))
      expect(mic.disabled).toBe(true)
      fireEvent.click(mic)
    }

    act(() => pushState?.(state()))
    const target = screen.getByRole('combobox', { name: 'Target session' })

    for (const nonCurrentTarget of ['new', 'stored-1']) {
      fireEvent.change(target, { target: { value: nonCurrentTarget } })
      expect(mic.disabled).toBe(true)
      fireEvent.click(mic)
    }

    expect(startVoice).not.toHaveBeenCalled()
  })

  it.each([
    ['transcribing', 'Transcribing'],
    ['thinking', 'Thinking'],
    ['speaking', 'Speaking']
  ] as const)('renders %s lifecycle status', (status, label) => {
    render(<QuickEntryApp />)
    act(() => pushState?.(state({ voice: { active: true, available: true, error: null, status } })))

    expect(screen.getByRole('status').textContent).toBe(label)
  })

  it('keeps connected text entry usable when voice is unavailable', () => {
    render(<QuickEntryApp />)
    act(() =>
      pushState?.(
        state({
          currentVoiceTargetAvailable: false,
          voice: { active: false, available: true, error: null, status: 'idle' }
        })
      )
    )

    const input = screen.getByRole('textbox', { name: 'Quick Entry' }) as HTMLInputElement
    expect(input.disabled).toBe(false)
    expect(screen.getByRole('status').textContent).toBe('Voice unavailable')
    fireEvent.change(input, { target: { value: 'still send text' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    expect(submit).toHaveBeenCalledWith({ target: 'current', text: 'still send text' })
    expect(startVoice).not.toHaveBeenCalled()
  })
})
