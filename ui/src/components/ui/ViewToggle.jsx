import React from 'react'
import { useViewStore } from '@/stores/useViewStore'

export default function ViewToggle() {
    const viewMode = useViewStore((s) => s.viewMode)
    const setView = useViewStore((s) => s.setView)

    return (
        <div className="flex items-center gap-1 glass rounded-lg p-1 bg-white/50 dark:bg-white/5 border border-slate-200 dark:border-white/10 backdrop-blur-md">
            <button
                onClick={() => setView('bento')}
                className={`flex items-center gap-2 px-3 py-1.5 rounded-md text-xs font-mono tracking-wider transition-all duration-300 ${viewMode === 'bento'
                    ? 'bg-indigo-100 text-indigo-700 shadow-sm dark:bg-white/10 dark:text-neon-cyan dark:shadow-[0_0_12px_rgba(6,182,212,0.2)]'
                    : 'text-slate-500 hover:text-slate-900 dark:text-white/30 dark:hover:text-white/50'
                    }`}
                title="Dashboard View"
            >
                {/* Grid icon */}
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <rect x="1" y="1" width="6" height="6" rx="1" stroke="currentColor" strokeWidth="1.2" />
                    <rect x="9" y="1" width="6" height="6" rx="1" stroke="currentColor" strokeWidth="1.2" />
                    <rect x="1" y="9" width="6" height="6" rx="1" stroke="currentColor" strokeWidth="1.2" />
                    <rect x="9" y="9" width="6" height="6" rx="1" stroke="currentColor" strokeWidth="1.2" />
                </svg>
                <span className="hidden sm:inline">Dashboard</span>
            </button>

            <button
                onClick={() => setView('hive')}
                className={`flex items-center gap-2 px-3 py-1.5 rounded-md text-xs font-mono tracking-wider transition-all duration-300 ${viewMode === 'hive'
                    ? 'bg-cyan-100 text-cyan-700 shadow-sm dark:bg-white/10 dark:text-neon-cyan dark:shadow-[0_0_12px_rgba(6,182,212,0.2)]'
                    : 'text-slate-500 hover:text-slate-900 dark:text-white/30 dark:hover:text-white/50'
                    }`}
                title="Hive View"
            >
                {/* Octagon icon */}
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                    <path d="M5 1H11L15 5V11L11 15H5L1 11V5L5 1Z" stroke="currentColor" strokeWidth="1.2" />
                </svg>
                <span className="hidden sm:inline">Hive</span>
            </button>
        </div>
    )
}
