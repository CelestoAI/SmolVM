import React from 'react'

export default function NebulaHUD({ stats }) {
    return (
        <div className="absolute inset-0 pointer-events-none z-10 p-8 flex flex-col justify-between">
            {/* Top section */}
            <div className="flex justify-between items-start">
                {/* Left: Big counter */}
                <div>
                    <h1 className="text-6xl font-light tracking-wider text-slate-900 dark:text-white/90 font-mono">
                        {stats.active.toLocaleString()}
                    </h1>
                    <p className="text-sm font-light tracking-[0.3em] text-slate-500 dark:text-white/50 uppercase mt-1">
                        Active Nodes
                    </p>
                    <p className="text-xs font-mono text-slate-400 dark:text-white/30 mt-3 tracking-widest">
                        GLOBAL LOAD: {stats.avgLoad}%
                    </p>
                </div>

                {/* Right: Brand */}
                <div className="text-right pt-10">
                    <p className="text-sm font-mono tracking-[0.3em] text-slate-400 dark:text-white/40">
                        SMOL::VM OS
                    </p>
                    <div className="flex gap-4 mt-4 text-xs font-mono text-slate-300 dark:text-white/25 tracking-wider">
                        <span>STOPPED: {stats.stopped ?? stats.error}</span>
                        <span>SUSPENDED: {stats.suspended ?? stats.idle}</span>
                    </div>
                </div>
            </div>

            {/* Bottom right: decorative sparkle */}
            <div className="flex justify-end">
                <svg
                    width="24"
                    height="24"
                    viewBox="0 0 24 24"
                    fill="none"
                    className="text-slate-300 dark:text-white/20"
                >
                    <path
                        d="M12 2L14.5 9.5L22 12L14.5 14.5L12 22L9.5 14.5L2 12L9.5 9.5L12 2Z"
                        fill="currentColor"
                    />
                </svg>
            </div>
        </div>
    )
}
