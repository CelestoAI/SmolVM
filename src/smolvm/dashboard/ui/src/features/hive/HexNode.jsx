import React from 'react'
import { motion } from 'framer-motion'
import { useViewStore } from '@/stores/useViewStore'
import { getDotClassForStatus, normalizeStatus } from '@/utils/status'

const CELL_PATH = 'polygon(30% 0%, 70% 0%, 100% 30%, 100% 70%, 70% 100%, 30% 100%, 0% 70%, 0% 30%)'

export default function HexNode({ node, isCompact = false }) {
    const setSelectedVM = useViewStore((s) => s.setSelectedVM)
    const statusKey = normalizeStatus(node.status)
    const isStopped = statusKey === 'stopped'
    const failedAnimationClass = isStopped
        ? (isCompact ? 'animate-compact-fail-pulse' : 'animate-failed-shell')
        : ''
    const idSuffix = node.id.includes('-') ? node.id.split('-').pop() : node.id

    const shellClass = isStopped
        ? 'border-rose-400/86 dark:border-rose-300/86 from-rose-300/88 via-rose-200/68 to-red-300/80 dark:from-rose-500/62 dark:via-rose-500/48 dark:to-red-500/56 shadow-[0_0_24px_rgba(225,29,72,0.36)] dark:shadow-[0_0_34px_rgba(251,113,133,0.62)]'
        : 'border-cyan-300/28 dark:border-cyan-300/20 from-cyan-100/74 via-sky-100/58 to-blue-100/44 dark:from-[#0f2238]/88 dark:via-[#102944]/90 dark:to-[#123154]/86 shadow-[0_0_12px_rgba(6,182,212,0.16)] dark:shadow-[0_0_18px_rgba(34,211,238,0.22)]'

    return (
        <motion.button
            type="button"
            layoutId={`node-${node.id}`}
            className="hexagon-wrapper relative w-24 h-24 flex items-center justify-center cursor-pointer group p-0 bg-transparent border-0"
            whileHover={{ scale: 1.08, zIndex: 10 }}
            whileTap={{ scale: 0.96 }}
            onClick={() => setSelectedVM(node.id)}
            aria-label={`Open VM ${node.id} details`}
        >
            <div
                className={`
                    absolute inset-0 rounded-[12px] blur-md transition-opacity duration-300
                    ${isStopped ? 'bg-rose-500/45 dark:bg-rose-500/56' : 'bg-cyan-300/24 dark:bg-cyan-400/14'}
                    opacity-70 group-hover:opacity-90
                `}
            />

            <div
                className={`
                    absolute inset-0 border
                    bg-gradient-to-b backdrop-blur-md transition-all duration-300
                    ${shellClass}
                    ${failedAnimationClass}
                `}
                style={{ clipPath: CELL_PATH }}
            />

            <div
                className="absolute inset-[6px] opacity-45 group-hover:opacity-70 transition-opacity duration-300"
                style={{
                    clipPath: CELL_PATH,
                    background: isStopped
                        ? 'radial-gradient(circle at 50% 40%, rgba(251,113,133,0.58), transparent 74%)'
                        : 'radial-gradient(circle at 50% 35%, rgba(34,211,238,0.3), transparent 74%)',
                }}
            />

            <div className="relative z-10 flex flex-col items-center justify-center pointer-events-none">
                <div
                    className={`
                        ${isStopped ? 'w-2 h-2 mb-2.5' : 'w-1.5 h-1.5 mb-2'}
                        rounded-full transition-all duration-300
                        ${getDotClassForStatus(node.status)}
                        ${isStopped && !isCompact ? 'animate-pulse-error' : ''}
                    `}
                />

                <span
                    className={`text-[11px] font-mono font-bold tracking-[0.14em] uppercase transition-colors duration-300 drop-shadow-[0_0_6px_rgba(15,23,42,0.35)] dark:drop-shadow-[0_0_7px_rgba(34,211,238,0.28)] ${
                        isStopped ? 'text-rose-900 dark:text-rose-100' : 'text-slate-800 dark:text-cyan-100'
                    }`}
                >
                    {idSuffix}
                </span>

                <span className={`text-[8px] font-mono mt-1 opacity-0 group-hover:opacity-100 transition-opacity duration-300 uppercase tracking-[0.15em] ${isStopped ? 'text-rose-700 dark:text-rose-100/86' : 'text-slate-600 dark:text-cyan-100/78'}`}>
                    {node.status}
                </span>
            </div>
        </motion.button>
    )
}
