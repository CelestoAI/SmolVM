import React, { useEffect } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { useViewStore } from '@/stores/useViewStore'
import ThemeToggle from '@/components/ui/ThemeToggle'
import ViewToggle from '@/components/ui/ViewToggle'
import CommandBar from '@/components/ui/CommandBar'
import HexGrid from '@/features/hive/HexGrid'
import NebulaHUD from '@/features/nebula/NebulaHUD'
import BentoGrid from '@/features/bento/BentoGrid'
import VMDetailCard from '@/features/nebula/VMDetailCard'

const pageVariants = {
    initial: { opacity: 0, scale: 0.98 },
    animate: { opacity: 1, scale: 1 },
    exit: { opacity: 0, scale: 1.02 },
}

const pageTransition = {
    duration: 0.5,
    ease: [0.25, 0.46, 0.45, 0.94],
}

export default function DashboardLayout({ nodes, stats }) {
    const viewMode = useViewStore((s) => s.viewMode)
    const selectedVMId = useViewStore((s) => s.selectedVM)
    const setSelectedVM = useViewStore((s) => s.setSelectedVM)
    const theme = useViewStore((s) => s.theme)

    // Sync theme with HTML root for Tailwind dark mode
    useEffect(() => {
        const root = window.document.documentElement
        if (theme === 'dark') {
            root.classList.add('dark')
        } else {
            root.classList.remove('dark')
        }
    }, [theme])

    // Find selected node data to pass to card
    const selectedNode = selectedVMId ? nodes.find(n => n.id === selectedVMId) : null

    return (
        <div className="relative w-full h-full bg-[var(--bg)] overflow-hidden transition-colors duration-500">
            {/* ─── Header ─── */}
            <header className="fixed top-0 left-0 right-0 z-40 flex items-center justify-between px-6 py-4 pointer-events-none bg-gradient-to-b from-white/80 via-white/35 to-transparent dark:from-black/45 dark:via-black/15">
                {/* Left: Brand */}
                <div className="flex items-center gap-3 pointer-events-auto">
                    {/* Logo mark */}
                    <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-sky-400/25 to-blue-400/25 dark:from-sky-300/24 dark:to-blue-300/24 border border-sky-300/35 dark:border-sky-200/12 flex items-center justify-center">
                        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                            <circle cx="7" cy="7" r="3" stroke="currentColor" className="text-sky-600 dark:text-sky-300" strokeWidth="1.5" />
                            <circle cx="7" cy="7" r="6" stroke="currentColor" className="text-cyan-600 dark:text-cyan-300" strokeWidth="0.5" opacity="0.4" />
                        </svg>
                    </div>
                    <span className="text-sm font-mono tracking-[0.25em] text-slate-400 dark:text-white/50 font-light">
                        SmolVM
                    </span>
                </div>

                {/* Center: View Controls */}
                <div className="flex items-center gap-4 pointer-events-auto">
                    <ThemeToggle />
                    <ViewToggle />
                </div>

                {/* Right: Live clock + status (hidden in hive to avoid overlap with HUD panel) */}
                {viewMode === 'hive' ? (
                    <div className="w-[180px] pointer-events-none" />
                ) : (
                    <div className="flex items-center gap-4 pointer-events-auto">
                        <div className="flex items-center gap-2">
                            <span className="w-1.5 h-1.5 rounded-full bg-sky-500 dark:bg-sky-300 animate-pulse" />
                            <span className="text-[10px] font-mono tracking-wider text-slate-400 dark:text-white/30">
                                {stats.total.toLocaleString()} NODES
                            </span>
                        </div>
                        <div className="text-[10px] font-mono text-slate-300 dark:text-white/20 tracking-wider">
                            {new Date().toLocaleTimeString('en-US', { hour12: false })}
                        </div>
                    </div>
                )}
            </header>

            {/* ─── Views ─── */}
            <motion.div
                key={viewMode}
                variants={pageVariants}
                initial="initial"
                animate="animate"
                exit="exit"
                transition={pageTransition}
                className="absolute inset-0"
            >
                {viewMode === 'hive' ? (
                    <>
                        <HexGrid nodes={nodes} />
                        <NebulaHUD stats={stats} />
                    </>
                ) : (
                    <BentoGrid stats={stats} nodes={nodes} />
                )}
            </motion.div>

            {/* ─── Global Overlays ─── */}
            <AnimatePresence>
                {selectedNode && (
                    <VMDetailCard
                        vm={selectedNode}
                        onClose={() => setSelectedVM(null)}
                    />
                )}
            </AnimatePresence>

            {/* ─── Command Bar ─── */}
            <CommandBar />
        </div>
    )
}
