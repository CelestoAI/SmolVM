import React, { useState } from 'react'

export default function CommandBar() {
    const [value, setValue] = useState('')

    const handleSubmit = (e) => {
        e.preventDefault()
        if (!value.trim()) return
        console.log('[SmolVM] Command:', value)
        setValue('')
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
                            className="w-8 h-8 rounded-lg bg-slate-100 dark:bg-white/5 border border-slate-200 dark:border-white/10 flex items-center justify-center hover:bg-indigo-50 dark:hover:bg-neon-purple/20 hover:border-indigo-200 dark:hover:border-neon-purple/30 transition-all duration-300 group"
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

                {/* Subtle gradient glow beneath */}
                <div className="absolute -bottom-2 left-1/2 -translate-x-1/2 w-3/4 h-4 bg-indigo-500/10 dark:bg-neon-purple/10 blur-xl rounded-full" />
            </form>
        </div>
    )
}
