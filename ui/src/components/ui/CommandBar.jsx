import React, { useRef, useState } from 'react'

import { apiUrl } from '@/utils/api'

// The request never got an answer back: the dashboard may be down.
const TRANSPORT_ERROR = 'Could not reach the dashboard. Check that it is running, then try again.'
// The dashboard answered, but not with a reason we can show. Do not blame the
// connection here: it plainly worked.
const SERVER_ERROR = 'The dashboard could not run that command. Try again.'

// The command endpoint reports handled failures as {error}, while FastAPI's
// own 422 and 500 responses use {detail}. Take whichever is a plain string.
function serverMessage(payload) {
    for (const value of [payload?.error, payload?.detail]) {
        if (typeof value === 'string' && value.trim()) return value
    }
    return SERVER_ERROR
}

export default function CommandBar() {
    const [value, setValue] = useState('')
    const [pending, setPending] = useState(false)
    const [result, setResult] = useState('')
    const [error, setError] = useState('')
    // `pending` drives the disabled button, but it only updates on the next
    // render. A ref changes immediately, so it is the guard that decides
    // whether a request goes out.
    const inFlight = useRef(false)

    const handleSubmit = async (e) => {
        e.preventDefault()
        const submitted = value
        const text = submitted.trim()
        if (!text || inFlight.current) return

        inFlight.current = true
        setPending(true)
        setResult('')
        setError('')

        try {
            const response = await fetch(apiUrl('/api/command'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text }),
            })
            // An unhandled server error answers with plain text, not JSON, so
            // parse defensively. Reaching here at all means the dashboard
            // answered, and the message below must not claim otherwise.
            const payload = await response.json().catch(() => null)

            if (!response.ok) {
                // The server sends a curated one-line reason; show it as-is.
                setError(serverMessage(payload))
                return
            }
            if (typeof payload?.result !== 'string') {
                setError(SERVER_ERROR)
                return
            }

            setResult(payload.result)
            // The input stays editable while the request runs, so only clear
            // it if the user has not started typing something else.
            setValue((current) => (current === submitted ? '' : current))
        } catch {
            // The request never completed, so there is nothing to read. Keep
            // the typed command so the user can retry without retyping it.
            setError(TRANSPORT_ERROR)
        } finally {
            inFlight.current = false
            setPending(false)
        }
    }

    return (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 w-full max-w-xl px-4">
            <form onSubmit={handleSubmit} className="relative">
                <div className="glass rounded-2xl glow-purple overflow-hidden bg-white/50 dark:bg-black/50 backdrop-blur-md border border-slate-200 dark:border-white/10">
                    <div className="flex items-center gap-3 px-5 py-3.5">
                        {/* Prompt chevron */}
                        <span className="text-slate-400 dark:text-white/20 font-mono text-sm select-none">›</span>

                        <input
                            type="text"
                            value={value}
                            onChange={(e) => setValue(e.target.value)}
                            placeholder="type command or search fleet..."
                            className="flex-1 bg-transparent text-sm font-mono text-slate-800 dark:text-white/80 placeholder:text-slate-400 dark:placeholder:text-white/20 outline-none tracking-wide"
                        />

                        {/* AI chip icon */}
                        <button
                            type="submit"
                            disabled={pending}
                            aria-label="Run command"
                            className="w-8 h-8 rounded-lg bg-slate-100 dark:bg-white/5 border border-slate-200 dark:border-white/10 flex items-center justify-center hover:bg-indigo-50 dark:hover:bg-neon-purple/20 hover:border-indigo-200 dark:hover:border-neon-purple/30 transition-all duration-300 group disabled:opacity-40 disabled:cursor-not-allowed"
                        >
                            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="text-slate-400 dark:text-white/30 group-hover:text-indigo-500 dark:group-hover:text-neon-purple transition-colors">
                                <rect x="3" y="3" width="10" height="10" rx="2" stroke="currentColor" strokeWidth="1.2" />
                                <circle cx="6" cy="6" r="1" fill="currentColor" />
                                <circle cx="10" cy="6" r="1" fill="currentColor" />
                                <circle cx="6" cy="10" r="1" fill="currentColor" />
                                <circle cx="10" cy="10" r="1" fill="currentColor" />
                                <line x1="8" y1="0" x2="8" y2="3" stroke="currentColor" strokeWidth="1" />
                                <line x1="13" y1="13" x2="16" y2="16" stroke="currentColor" strokeWidth="1" />
                                <line x1="0" y1="8" x2="3" y2="8" stroke="currentColor" strokeWidth="1" />
                                <line x1="13" y1="8" x2="16" y2="8" stroke="currentColor" strokeWidth="1" />
                            </svg>
                        </button>
                    </div>
                </div>

                {/* Command outcome. Rendered as text so a server response can
                    never inject markup into the dashboard. */}
                <div
                    role="status"
                    aria-live="polite"
                    className="mt-2 px-2 text-xs font-mono text-slate-600 dark:text-white/60"
                >
                    {result}
                </div>
                <div
                    role="alert"
                    className="mt-1 px-2 text-xs font-mono text-rose-600 dark:text-rose-400"
                >
                    {error}
                </div>

                {/* Subtle gradient glow beneath */}
                <div className="absolute -bottom-2 left-1/2 -translate-x-1/2 w-3/4 h-4 bg-indigo-500/10 dark:bg-neon-purple/10 blur-xl rounded-full" />
            </form>
        </div>
    )
}
