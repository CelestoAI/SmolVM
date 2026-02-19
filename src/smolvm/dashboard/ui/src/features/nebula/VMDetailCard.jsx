import React, { useMemo, useState } from 'react'
import { normalizeStatus } from '@/utils/status'

function getApiBaseUrl() {
    const envBase = import.meta.env.VITE_API_BASE_URL
    if (envBase) {
        return String(envBase).replace(/\/$/, '')
    }

    if (typeof window === 'undefined') {
        return ''
    }

    const { protocol, hostname, port } = window.location
    if (port === '5173') {
        return `${protocol}//${hostname}:8000`
    }

    return ''
}

export default function VMDetailCard({ vm, onClose }) {
    if (!vm) return null

    const apiBase = useMemo(() => getApiBaseUrl(), [])
    const [pendingAction, setPendingAction] = useState(null)
    const [actionError, setActionError] = useState('')
    const normalizedStatus = normalizeStatus(vm.status)
    const statusLabel = normalizedStatus.toUpperCase()
    const cpuPercent = Math.min(Math.round(((Number(vm.vcpu) || 0) / 32) * 100), 100)
    const memoryPercent = Math.min(Math.round(((Number(vm.memory) || 0) / 16384) * 100), 100)
    const isStopped = normalizedStatus === 'stopped'
    const isBusy = pendingAction !== null
    const safeValue = (value, fallback = 'N/A') => {
        if (value === null || value === undefined || value === '') return fallback
        return value
    }

    const runAction = async (kind) => {
        if (isBusy) return
        setActionError('')
        setPendingAction(kind)

        try {
            const vmId = encodeURIComponent(vm.id)
            const endpoint = kind === 'stop' ? `/api/vms/${vmId}/stop` : `/api/vms/${vmId}`
            const method = kind === 'stop' ? 'POST' : 'DELETE'
            const response = await fetch(`${apiBase}${endpoint}`, {
                method,
                headers: { Accept: 'application/json' },
            })

            if (!response.ok) {
                let detail = `Request failed (${response.status})`
                try {
                    const payload = await response.json()
                    if (payload?.detail) {
                        detail = String(payload.detail)
                    } else if (payload?.error) {
                        detail = String(payload.error)
                    }
                } catch {
                    // Ignore JSON parse failures.
                }
                throw new Error(detail)
            }

            onClose()
        } catch (error) {
            const message = error instanceof Error ? error.message : 'Action failed'
            setActionError(message)
        } finally {
            setPendingAction(null)
        }
    }

    const getStatusColor = (s) => {
        switch (normalizeStatus(s)) {
            case 'active':
                return 'bg-emerald-500 dark:bg-emerald-400'
            case 'starting':
                return 'bg-sky-500 dark:bg-sky-400'
            case 'suspended':
                return 'bg-amber-500 dark:bg-amber-400'
            case 'stopped':
                return 'bg-rose-500 dark:bg-rose-400'
            default:
                return 'bg-slate-400 dark:bg-slate-500'
        }
    }

    return (
        <div className="absolute top-24 right-8 z-50 w-80 glass rounded-xl overflow-hidden border border-slate-200 dark:border-white/10 animate-in fade-in slide-in-from-right-4 duration-300 shadow-xl">
            {/* Header */}
            <div className="bg-slate-100/50 dark:bg-white/5 px-4 py-3 flex items-center justify-between border-b border-slate-200 dark:border-white/5">
                <div className="flex items-center gap-3">
                    <div className={`w-2 h-2 rounded-full ${getStatusColor(vm.status)} animate-pulse`} />
                    <span className="font-mono text-sm tracking-wider text-slate-700 dark:text-white/90">{vm.id}</span>
                </div>
                <button onClick={onClose} className="text-slate-400 dark:text-white/30 hover:text-slate-600 dark:hover:text-white transition-colors">
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M12 4L4 12" stroke="currentColor" /><path d="M4 4L12 12" stroke="currentColor" /></svg>
                </button>
            </div>

            {/* Content */}
            <div className="p-4 space-y-4">
                {/* Info Grid */}
                <div className="grid grid-cols-2 gap-4 text-xs font-mono">
                    <div>
                        <p className="text-slate-400 dark:text-white/30 uppercase tracking-widest mb-1">Status</p>
                        <p className="text-slate-700 dark:text-white/80">{statusLabel}</p>
                    </div>
                    <div>
                        <p className="text-slate-400 dark:text-white/30 uppercase tracking-widest mb-1">PID</p>
                        <p className="text-slate-700 dark:text-white/80">{safeValue(vm.pid)}</p>
                    </div>
                    <div>
                        <p className="text-slate-400 dark:text-white/30 uppercase tracking-widest mb-1">Guest IP</p>
                        <p className="text-slate-700 dark:text-white/80">{safeValue(vm.ip, 'unassigned')}</p>
                    </div>
                    <div>
                        <p className="text-slate-400 dark:text-white/30 uppercase tracking-widest mb-1">Gateway</p>
                        <p className="text-slate-700 dark:text-white/80">{safeValue(vm.gateway)}</p>
                    </div>
                    <div>
                        <p className="text-slate-400 dark:text-white/30 uppercase tracking-widest mb-1">Tap Device</p>
                        <p className="text-slate-700 dark:text-white/80">{safeValue(vm.tapDevice)}</p>
                    </div>
                    <div>
                        <p className="text-slate-400 dark:text-white/30 uppercase tracking-widest mb-1">SSH Port</p>
                        <p className="text-slate-700 dark:text-white/80">{safeValue(vm.sshHostPort)}</p>
                    </div>
                </div>

                {/* Resources */}
                <div className="space-y-3 pt-2 border-t border-slate-200 dark:border-white/5">
                    <div>
                        <div className="flex justify-between text-[10px] text-slate-400 dark:text-white/40 mb-1 uppercase tracking-wider">
                            <span>vCPU Allocation</span>
                            <span>{safeValue(vm.vcpu, 0)} / 32</span>
                        </div>
                        <div className="h-1 bg-slate-200 dark:bg-white/10 rounded-full overflow-hidden">
                            <div
                                className="h-full bg-cyan-500 dark:bg-neon-cyan"
                                style={{ width: `${cpuPercent}%` }}
                            />
                        </div>
                    </div>
                    <div>
                        <div className="flex justify-between text-[10px] text-slate-400 dark:text-white/40 mb-1 uppercase tracking-wider">
                            <span>Memory Allocation</span>
                            <span>{safeValue(vm.memory, 0)} MiB</span>
                        </div>
                        <div className="h-1 bg-slate-200 dark:bg-white/10 rounded-full overflow-hidden">
                            <div
                                className="h-full bg-indigo-500 dark:bg-neon-purple"
                                style={{ width: `${memoryPercent}%` }}
                            />
                        </div>
                    </div>
                </div>

                {/* Actions */}
                <div className="grid grid-cols-2 gap-2 pt-2">
                    <button
                        onClick={() => runAction('stop')}
                        disabled={isBusy || isStopped}
                        className={`px-3 py-1.5 rounded text-[10px] font-mono tracking-wider border transition-colors ${
                            isBusy || isStopped
                                ? 'bg-slate-100 dark:bg-white/5 text-slate-400 dark:text-white/30 border-slate-200 dark:border-white/10 cursor-not-allowed'
                                : 'bg-amber-50 dark:bg-amber-500/10 hover:bg-amber-100 dark:hover:bg-amber-500/20 text-amber-700 dark:text-amber-300 border-amber-200 dark:border-amber-400/20'
                        }`}
                    >
                        {pendingAction === 'stop' ? 'STOPPING...' : 'STOP'}
                    </button>
                    <button
                        onClick={() => runAction('delete')}
                        disabled={isBusy}
                        className={`px-3 py-1.5 rounded text-[10px] font-mono tracking-wider border transition-colors ${
                            isBusy
                                ? 'bg-slate-100 dark:bg-white/5 text-slate-400 dark:text-white/30 border-slate-200 dark:border-white/10 cursor-not-allowed'
                                : 'bg-rose-50 dark:bg-neon-rose/10 hover:bg-rose-100 dark:hover:bg-neon-rose/20 text-rose-600 dark:text-neon-rose border-rose-200 dark:border-neon-rose/20'
                        }`}
                    >
                        {pendingAction === 'delete' ? 'DELETING...' : 'KILL'}
                    </button>
                </div>

                {actionError && (
                    <div className="text-[10px] font-mono text-rose-600 dark:text-rose-300 tracking-wide">
                        {actionError}
                    </div>
                )}

                <div className="grid grid-cols-2 gap-2">
                    <div className="px-3 py-1.5 rounded bg-slate-100 dark:bg-white/5 text-[10px] font-mono tracking-wider text-slate-600 dark:text-white/70 border border-slate-200 dark:border-white/5">
                        VM: {vm.id}
                    </div>
                    <button onClick={onClose} className="px-3 py-1.5 rounded bg-rose-50 dark:bg-neon-rose/10 hover:bg-rose-100 dark:hover:bg-neon-rose/20 text-[10px] font-mono tracking-wider text-rose-600 dark:text-neon-rose border border-rose-200 dark:border-neon-rose/20">
                        CLOSE
                    </button>
                </div>
            </div>
        </div>
    )
}
