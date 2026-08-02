import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import CommandBar from './CommandBar'

const TRANSPORT_ERROR =
  'Could not reach the dashboard. Check that it is running, then try again.'
const SERVER_ERROR = 'The dashboard could not run that command. Try again.'

function jsonResponse(body, { ok = true } = {}) {
  return { ok, json: async () => body }
}

function typeCommand(text) {
  const input = screen.getByPlaceholderText('type command or search fleet...')
  fireEvent.change(input, { target: { value: text } })
  return input
}

describe('CommandBar', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('posts submitted commands to the dashboard command endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ action: 'list', target: '', result: 'Found 2 VMs.', affected_vms: [] }),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('  LIST  ')
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: 'LIST' }),
      })
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('targets the dashboard server when the UI is served by Vite on 5173', async () => {
    // In development the page is on 5173 but the API is on 8000, so a bare
    // relative path would ask Vite for the API and get the page back.
    const original = window.location
    delete window.location
    window.location = { protocol: 'http:', hostname: 'localhost', port: '5173' }

    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ action: 'list', target: '', result: 'ok', affected_vms: [] }),
    )
    vi.stubGlobal('fetch', fetchMock)

    try {
      render(<CommandBar />)
      const input = typeCommand('LIST')
      fireEvent.submit(input.closest('form'))

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          'http://localhost:8000/api/command',
          expect.objectContaining({ method: 'POST' }),
        )
      })
    } finally {
      window.location = original
    }
  })

  it('shows the result in a status region and clears the input on success', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ action: 'list', target: '', result: 'Found 2 VMs.', affected_vms: [] }),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('LIST')
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toBe('Found 2 VMs.')
    })
    expect(input.value).toBe('')
  })

  it('renders a server error as text in an alert and keeps the command', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ error: 'Unknown command: <b>oops</b>' }, { ok: false }),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('oops')
    fireEvent.submit(input.closest('form'))

    const alert = await screen.findByRole('alert')
    await waitFor(() => {
      expect(alert.textContent).toBe('Unknown command: <b>oops</b>')
    })
    // Escaped, not parsed: the response never becomes dashboard markup.
    expect(alert.querySelector('b')).toBeNull()
    expect(screen.getByRole('status').textContent).toBe('')
    expect(input.value).toBe('oops')
  })

  it('does not blame the connection when the server answered without an error field', async () => {
    // FastAPI's own 422 body uses {detail: [...]}, which is not a string.
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ detail: [{ msg: 'field required', loc: ['body', 'text'] }] }, { ok: false }),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('LIST')
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toBe(SERVER_ERROR)
    })
    expect(input.value).toBe('LIST')
  })

  it('shows the server detail string when that is what came back', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ detail: 'VM not found: sbx-einstein' }, { ok: false }),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('info sbx-einstein')
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toBe('VM not found: sbx-einstein')
    })
  })

  it('shows a plain-English retry message when the request fails', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('LIST')
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toBe(TRANSPORT_ERROR)
    })
    expect(input.value).toBe('LIST')
  })

  it('does not blame the connection when the answer was not JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => {
        throw new SyntaxError('Unexpected token < in JSON')
      },
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('LIST')
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toBe(SERVER_ERROR)
    })
    expect(input.value).toBe('LIST')
  })

  it('sends no request for blank input', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('   ')
    fireEvent.submit(input.closest('form'))

    await Promise.resolve()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('keeps text the user typed while the request was in flight', async () => {
    let release
    const inFlight = new Promise((resolve) => {
      release = resolve
    })
    const fetchMock = vi.fn().mockReturnValue(inFlight)
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('LIST')
    fireEvent.submit(input.closest('form'))

    // The input stays editable while the button is disabled.
    fireEvent.change(input, { target: { value: 'info sbx-1' } })

    release(jsonResponse({ action: 'list', target: '', result: 'Found 2 VMs.', affected_vms: [] }))
    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toBe('Found 2 VMs.')
    })
    // The success belonged to the previous command; do not wipe the new text.
    expect(input.value).toBe('info sbx-1')
  })

  it('reports a missing sandbox as a failure, not a result', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        { error: "No sandbox named 'sbx-nope'. Run 'list' to see the ones you have." },
        { ok: false },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('info sbx-nope')
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toBe(
        "No sandbox named 'sbx-nope'. Run 'list' to see the ones you have.",
      )
    })
    expect(screen.getByRole('status').textContent).toBe('')
    expect(input.value).toBe('info sbx-nope')
  })

  it('sends one request when two submits run before React re-renders', async () => {
    let release
    const inFlight = new Promise((resolve) => {
      release = resolve
    })
    const fetchMock = vi.fn().mockReturnValue(inFlight)
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('LIST')
    const form = input.closest('form')
    // Two requestSubmit() calls in the same task: both handlers close over the
    // pre-update `pending`, so only a guard that updates immediately stops the
    // second request.
    await act(async () => {
      form.requestSubmit()
      form.requestSubmit()
    })

    expect(fetchMock).toHaveBeenCalledTimes(1)

    release(jsonResponse({ action: 'list', target: '', result: 'ok', affected_vms: [] }))
    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toBe('ok')
    })
  })

  it('disables submit while a request is pending so it cannot double-send', async () => {
    let release
    const inFlight = new Promise((resolve) => {
      release = resolve
    })
    const fetchMock = vi.fn().mockReturnValue(inFlight)
    vi.stubGlobal('fetch', fetchMock)

    render(<CommandBar />)

    const input = typeCommand('LIST')
    const submit = screen.getByRole('button', { name: 'Run command' })
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(submit.disabled).toBe(true)
    })

    fireEvent.submit(input.closest('form'))
    expect(fetchMock).toHaveBeenCalledTimes(1)

    release(
      jsonResponse({ action: 'list', target: '', result: 'Found 0 VMs.', affected_vms: [] }),
    )
    await waitFor(() => {
      expect(submit.disabled).toBe(false)
    })
  })
})
