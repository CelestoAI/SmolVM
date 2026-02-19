import React, { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { useViewStore } from '@/stores/useViewStore'
import { getDotClassForStatus, normalizeStatus } from '@/utils/status'

// --- Components ---

function Card({ children, className = '', span = 'col-span-3' }) {
    return (
        <div className={`glass rounded-xl p-5 ${span} ${className} hover:border-slate-300 dark:hover:border-slate-500/90 transition-all duration-300 group shadow-lg dark:shadow-[0_14px_34px_rgba(2,6,23,0.5)] bg-white/82 dark:bg-slate-900/88 border border-slate-200 dark:border-slate-600/80`}>
            {children}
        </div>
    )
}

function CardHeader({ title, icon, children }) {
    return (
        <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
                {icon && <span className="text-slate-400 dark:text-white/40">{icon}</span>}
                <h3 className="text-[10px] uppercase tracking-[0.2em] text-slate-500 dark:text-white/50 font-medium">{title}</h3>
            </div>
            {children}
        </div>
    )
}

// --- Icons ---

const CubeIcon = () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
        <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
        <line x1="12" y1="22.08" x2="12" y2="12" />
    </svg>
)

const NetworkIcon = () => (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" />
        <line x1="2" y1="12" x2="22" y2="12" />
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
)

export default function BentoGrid({ nodes, stats }) {
    const setSelectedVM = useViewStore((s) => s.setSelectedVM)
    const safeNodes = Array.isArray(nodes) ? nodes : []
    const totalNodes = safeNodes.length
    const ratioBase = Math.max(totalNodes, 1)

    // Lazy Loading State
    const [visibleCount, setVisibleCount] = useState(50)
    const scrollContainerRef = useRef(null)
    const loaderRef = useRef(null)

    const displayNodes = safeNodes.slice(0, visibleCount)

    const handleObserver = useCallback((entries) => {
        const target = entries[0]
        if (target.isIntersecting) {
            setVisibleCount((prev) => Math.min(prev + 50, totalNodes))
        }
    }, [totalNodes])

    useEffect(() => {
        if (typeof window === 'undefined' || !('IntersectionObserver' in window)) {
            setVisibleCount(totalNodes)
            return
        }

        const option = {
            root: scrollContainerRef.current,
            rootMargin: "20px",
            threshold: 0
        }
        const observer = new IntersectionObserver(handleObserver, option)
        if (loaderRef.current) observer.observe(loaderRef.current)

        return () => {
            if (loaderRef.current) observer.unobserve(loaderRef.current)
        }
    }, [handleObserver, totalNodes])

    // Calculate real stats from nodes
    const statusCounts = safeNodes.reduce((acc, node) => {
        const bucket = normalizeStatus(node.status)
        acc[bucket] = (acc[bucket] || 0) + 1
        return acc
    }, {
        active: 0,
        starting: 0,
        suspended: 0,
        stopped: 0,
        unknown: 0,
    })

    const activeCount = statusCounts.active
    const startingCount = statusCounts.starting
    const suspendedCount = statusCounts.suspended
    const stoppedCount = statusCounts.stopped
    const activeRatio = totalNodes > 0 ? Math.round((activeCount / totalNodes) * 100) : 0
    const gaugeDashOffset = 351 * (1 - (activeRatio / 100))

    const runtimeRows = useMemo(() => {
        return safeNodes.slice(0, 14).map((node) => ({
            id: node.id,
            status: normalizeStatus(node.status),
            ip: node.ip || 'unassigned',
            tapDevice: node.tapDevice || 'n/a',
        }))
    }, [safeNodes])

    return (
        <div ref={scrollContainerRef} className="absolute inset-0 overflow-y-auto overflow-x-hidden p-6 pt-24 pb-20 bento-surface bento-scroll">
            <div className="relative z-10 grid grid-cols-1 md:grid-cols-6 lg:grid-cols-12 gap-4 max-w-[1600px] mx-auto auto-rows-min">

                {/* --- Row 1: Fleet Status & Network --- */}

                {/* Fleet Status */}
                <Card span="col-span-1 md:col-span-3 lg:col-span-3" className="h-[17.25rem] lg:row-start-1">
                    <CardHeader title="Fleet Status" />
                    <div className="flex flex-col gap-6 mt-4">
                        {/* Simple Stat Bars */}
                        <div className="space-y-3">
                            <div>
                                <div className="flex justify-between text-xs mb-1">
                                    <span className="text-slate-500 dark:text-white/60">Active</span>
                                    <span className="font-mono text-emerald-600 dark:text-emerald-400">{activeCount}</span>
                                </div>
                                <div className="h-1.5 bg-slate-200 dark:bg-white/5 rounded-full overflow-hidden">
                                    <div className="h-full bg-emerald-500 dark:bg-emerald-400" style={{ width: `${(activeCount / ratioBase) * 100}%` }} />
                                </div>
                            </div>
                            <div>
                                <div className="flex justify-between text-xs mb-1">
                                    <span className="text-slate-500 dark:text-white/60">Starting</span>
                                    <span className="font-mono text-sky-600 dark:text-sky-400">{startingCount}</span>
                                </div>
                                <div className="h-1.5 bg-slate-200 dark:bg-white/5 rounded-full overflow-hidden">
                                    <div className="h-full bg-sky-500 dark:bg-sky-400" style={{ width: `${(startingCount / ratioBase) * 100}%` }} />
                                </div>
                            </div>
                            <div>
                                <div className="flex justify-between text-xs mb-1">
                                    <span className="text-slate-500 dark:text-white/60">Suspended</span>
                                    <span className="font-mono text-amber-600 dark:text-amber-400">{suspendedCount}</span>
                                </div>
                                <div className="h-1.5 bg-slate-200 dark:bg-white/5 rounded-full overflow-hidden">
                                    <div className="h-full bg-amber-500 dark:bg-amber-400" style={{ width: `${(suspendedCount / ratioBase) * 100}%` }} />
                                </div>
                            </div>
                            <div>
                                <div className="flex justify-between text-xs mb-1">
                                    <span className="text-slate-500 dark:text-white/60">Stopped</span>
                                    <span className="font-mono text-rose-600 dark:text-rose-400">{stoppedCount}</span>
                                </div>
                                <div className="h-1.5 bg-slate-200 dark:bg-white/5 rounded-full overflow-hidden">
                                    <div className="h-full bg-rose-500 dark:bg-rose-400" style={{ width: `${(stoppedCount / ratioBase) * 100}%` }} />
                                </div>
                            </div>
                        </div>

                        <div className="border-t border-white/5 pt-4">
                            <div className="text-[10px] text-slate-400 dark:text-white/30 uppercase tracking-widest mb-1">Total Capacity</div>
                            <div className="text-xl font-light font-mono text-slate-700 dark:text-white/80">
                                {Math.round((activeCount / ratioBase) * 100)}% <span className="text-xs text-slate-400 dark:text-white/40">Efficiency</span>
                            </div>
                        </div>
                    </div>
                </Card>

                {/* MicroVM Cluster (Center Hero) */}
                <Card span="col-span-1 md:col-span-6 lg:col-span-6" className="relative overflow-hidden flex flex-col h-[35.5rem] lg:row-span-2 lg:row-start-1">
                    <CardHeader title="MicroVM Cluster Topology" icon={<CubeIcon />}>
                        <div className="text-[10px] font-mono text-slate-400 dark:text-white/30">
                            Viewing {displayNodes.length} of {totalNodes}
                        </div>
                    </CardHeader>

                    {/* List View Table */}
                    <div className="flex-1 overflow-auto bento-scroll relative">
                        <table className="w-full text-left border-collapse">
                            <thead className="text-[10px] uppercase font-mono text-slate-400 dark:text-white/30 tracking-wider sticky top-0 bg-slate-50/90 dark:bg-[#0a0e1a]/90 backdrop-blur-sm z-10">
                                <tr>
                                    <th className="py-2.5 pl-4 font-normal">Status</th>
                                    <th className="py-2.5 font-normal">ID</th>
                                    <th className="py-2.5 font-normal hidden sm:table-cell">IP Address</th>
                                    <th className="py-2.5 font-normal hidden md:table-cell">vCPU</th>
                                    <th className="py-2.5 font-normal hidden md:table-cell">Memory</th>
                                    <th className="py-2.5 pr-4 font-normal text-right">Actions</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100 dark:divide-white/5 text-xs font-mono">
                                {displayNodes.map((node) => (
                                    <tr
                                        key={node.id}
                                        className="group hover:bg-slate-50/50 dark:hover:bg-white/5 transition-colors cursor-pointer"
                                        onClick={() => setSelectedVM(node.id)}
                                    >
                                        <td className="py-2.5 pl-4">
                                            <div className="flex items-center gap-2">
                                                <div className={`w-1.5 h-1.5 rounded-full ${getDotClassForStatus(node.status)}`} />
                                            </div>
                                        </td>
                                        <td className="py-2.5 font-medium text-slate-700 dark:text-white/80">
                                            {node.id}
                                        </td>
                                        <td className="py-2.5 text-slate-500 dark:text-white/40 hidden sm:table-cell">
                                            {node.ip || 'unassigned'}
                                        </td>
                                        <td className="py-2.5 text-slate-500 dark:text-white/40 hidden md:table-cell">
                                            {node.vcpu || 0}
                                        </td>
                                        <td className="py-2.5 text-slate-500 dark:text-white/40 hidden md:table-cell">
                                            {node.memory || 0}MB
                                        </td>
                                        <td className="py-2.5 pr-4 text-right">
                                            <div className="opacity-0 group-hover:opacity-100 transition-opacity flex justify-end gap-2">
                                                <button className="p-1 hover:bg-slate-200 dark:hover:bg-white/10 rounded text-slate-400 hover:text-slate-600 dark:text-white/40 dark:hover:text-white">
                                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M23 4v6h-6" /><path d="M1 20v-6h6" /><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" /></svg>
                                                </button>
                                                <button className="p-1 hover:bg-rose-100 dark:hover:bg-rose-500/20 rounded text-slate-400 hover:text-rose-600 dark:text-white/40 dark:hover:text-rose-400">
                                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><line x1="15" y1="9" x2="9" y2="15" /><line x1="9" y1="9" x2="15" y2="15" /></svg>
                                                </button>
                                            </div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>

                        {/* Sentinel for infinite scroll */}
                        <div ref={loaderRef} className="h-4 w-full" />

                        {totalNodes > visibleCount && (
                            <div className="py-4 text-center text-xs text-slate-400 dark:text-white/30 font-mono">
                                Loading more...
                            </div>
                        )}
                    </div>
                </Card>

                {/* Network Info */}
                <Card span="col-span-1 md:col-span-3 lg:col-span-3" className="h-[17.25rem] lg:row-start-1">
                    <CardHeader title="Network Allocations" icon={<NetworkIcon />} />
                    <div className="space-y-2 mt-2 overflow-y-auto h-48 pr-2 bento-scroll">
                        {safeNodes.slice(0, 10).map((node) => (
                            <div key={node.id} className="flex items-center justify-between text-xs py-1 border-b border-slate-100 dark:border-white/5 last:border-0">
                                <div className="flex items-center gap-2">
                                    <div className={`w-1 h-1 rounded-full ${getDotClassForStatus(node.status)}`} />
                                    <span className="font-mono text-slate-600 dark:text-white/60">{node.id}</span>
                                </div>
                                <div className="font-mono text-slate-400 dark:text-white/40 text-[10px]">
                                    {node.ip || 'unassigned'}
                                </div>
                            </div>
                        ))}
                    </div>
                </Card>

                {/* --- Row 2 --- */}

                {/* Runtime Snapshot */}
                <Card span="col-span-1 md:col-span-3 lg:col-span-3" className="h-[17.25rem] lg:row-start-2">
                    <CardHeader title="Runtime Snapshot" />
                    <div className="space-y-2 font-mono text-[10px] h-48 overflow-y-auto bento-scroll">
                        {runtimeRows.length === 0 ? (
                            <div className="text-slate-400 dark:text-white/35 pt-2">No VM records available</div>
                        ) : (
                            runtimeRows.map((row) => (
                                <div key={row.id} className="flex items-center justify-between border-l-2 border-slate-200 dark:border-white/5 pl-2 py-1 hover:bg-slate-100 dark:hover:bg-white/5 transition-colors">
                                    <div className="flex items-center gap-2 min-w-0">
                                        <div className={`w-1.5 h-1.5 rounded-full ${getDotClassForStatus(row.status)}`} />
                                        <span className="text-slate-700 dark:text-white/70 truncate">{row.id}</span>
                                    </div>
                                    <div className="flex items-center gap-3 text-slate-500 dark:text-white/45 shrink-0">
                                        <span>{row.ip}</span>
                                        <span className="text-slate-400 dark:text-white/30">{row.tapDevice}</span>
                                    </div>
                                </div>
                            ))
                        )}
                    </div>
                </Card>

                <Card span="col-span-1 md:col-span-3 lg:col-span-3" className="h-[17.25rem] lg:row-start-2">
                    <CardHeader title="Fleet Health" />
                    {/* Simplified Gauge */}
                    <div className="flex items-center justify-center h-full pb-8">
                        <div className="relative w-32 h-32 flex items-center justify-center">
                            <svg className="w-full h-full transform -rotate-90">
                                <circle cx="64" cy="64" r="56" stroke="currentColor" className="text-slate-200 dark:text-white/5" strokeWidth="8" fill="none" />
                                <circle
                                    cx="64" cy="64" r="56"
                                    stroke="currentColor"
                                    strokeWidth="8"
                                    fill="none"
                                    strokeDasharray="351"
                                    strokeDashoffset={gaugeDashOffset}
                                    className="text-cyan-500 dark:text-[#06b6d4] transition-all duration-1000 ease-out"
                                />
                            </svg>
                            <div className="absolute inset-0 flex flex-col items-center justify-center">
                                <span className="text-2xl font-light text-slate-700 dark:text-white">{activeRatio}%</span>
                                <span className="text-[10px] text-slate-400 dark:text-white/40 uppercase tracking-widest">Active Ratio</span>
                            </div>
                        </div>
                    </div>
                    <div className="-mt-8 px-1 text-[10px] font-mono tracking-wider text-slate-500 dark:text-white/45 flex justify-between">
                        <span>AVG vCPU: {stats.avgCpu}</span>
                        <span>AVG MEM: {stats.avgMemory} MiB</span>
                    </div>
                </Card>

            </div>
        </div>
    )
}
