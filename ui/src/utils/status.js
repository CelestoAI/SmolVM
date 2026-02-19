export function normalizeStatus(status) {
    const value = String(status || '').toLowerCase()

    if (value === 'active' || value === 'running') return 'active'
    if (value === 'starting' || value === 'booting' || value === 'created' || value === 'snapshotting') return 'starting'
    if (value === 'suspended' || value === 'halting' || value === 'idle') return 'suspended'
    if (value === 'stopped' || value === 'error' || value === 'failed') return 'stopped'

    return 'unknown'
}

export function getDotClassForStatus(status) {
    const normalized = normalizeStatus(status)

    switch (normalized) {
        case 'active':
            return 'bg-emerald-500 dark:bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.9)]'
        case 'starting':
            return 'bg-sky-500 dark:bg-sky-400 shadow-[0_0_10px_rgba(56,189,248,0.9)]'
        case 'suspended':
            return 'bg-amber-500 dark:bg-amber-400 shadow-[0_0_10px_rgba(251,191,36,0.9)]'
        case 'stopped':
            return 'bg-rose-500 dark:bg-rose-400 shadow-[0_0_12px_rgba(251,113,133,0.95)]'
        default:
            return 'bg-slate-400 dark:bg-slate-300 shadow-[0_0_8px_rgba(148,163,184,0.45)]'
    }
}
