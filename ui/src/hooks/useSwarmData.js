import { useMemo, useEffect, useState, useCallback, useRef } from 'react'
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

function getWsUrl(apiBase) {
    if (typeof window === 'undefined') return null

    if (apiBase) {
        const url = new URL(apiBase)
        url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
        url.pathname = '/api/stream'
        url.search = ''
        url.hash = ''
        return url.toString()
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}/api/stream`
}

function mapVmToNode(vm) {
    const network = vm.network || {}
    const config = vm.config || {}
    const status = vm.status || 'unknown'

    return {
        id: vm.vm_id,
        status,
        statusNormalized: normalizeStatus(status),
        ip: network.guest_ip || 'unassigned',
        gateway: network.gateway_ip || null,
        tapDevice: network.tap_device || null,
        sshHostPort: network.ssh_host_port ?? null,
        pid: vm.pid ?? null,
        vcpu: config.vcpu_count ?? 0,
        memory: config.mem_size_mib ?? 0,
    }
}

export function useSwarmData() {
    const [nodes, setNodes] = useState([])
    const apiBase = useMemo(() => getApiBaseUrl(), [])
    const refreshTimerRef = useRef(null)
    const mountedRef = useRef(true)

    useEffect(() => {
        mountedRef.current = true
        return () => {
            mountedRef.current = false
        }
    }, [])

    const fetchVms = useCallback(async () => {
        const response = await fetch(`${apiBase}/api/vms`, {
            headers: { Accept: 'application/json' },
        })
        if (!response.ok) {
            throw new Error(`Failed to fetch VMs (${response.status})`)
        }

        const payload = await response.json()
        const mapped = Array.isArray(payload)
            ? payload.map(mapVmToNode).sort((a, b) => a.id.localeCompare(b.id))
            : []
        if (mountedRef.current) {
            setNodes(mapped)
        }
    }, [apiBase])

    useEffect(() => {
        const initialLoad = async () => {
            try {
                await fetchVms()
            } catch {
                if (mountedRef.current) {
                    setNodes([])
                }
            }
        }

        void initialLoad()
    }, [fetchVms])

    useEffect(() => {
        const wsUrl = getWsUrl(apiBase)
        if (!wsUrl) return undefined

        let socket = null
        let reconnectTimer = null
        let isClosing = false

        const scheduleRefresh = () => {
            if (refreshTimerRef.current) return
            refreshTimerRef.current = setTimeout(() => {
                refreshTimerRef.current = null
                void fetchVms().catch(() => {})
            }, 120)
        }

        const connect = () => {
            socket = new WebSocket(wsUrl)

            socket.onopen = () => {
                scheduleRefresh()
            }

            socket.onmessage = () => {
                scheduleRefresh()
            }

            socket.onerror = () => {
                if (socket) socket.close()
            }

            socket.onclose = () => {
                if (isClosing) return
                reconnectTimer = setTimeout(connect, 3000)
            }
        }

        connect()

        return () => {
            isClosing = true
            if (reconnectTimer) clearTimeout(reconnectTimer)
            if (refreshTimerRef.current) {
                clearTimeout(refreshTimerRef.current)
                refreshTimerRef.current = null
            }
            if (socket && socket.readyState < 2) {
                socket.close()
            }
        }
    }, [apiBase, fetchVms])

    return nodes
}

// Derived stats helper
export function useSwarmStats(nodes) {
    return useMemo(() => {
        const total = nodes.length
        const counts = nodes.reduce((acc, node) => {
            const key = normalizeStatus(node.status)
            acc[key] = (acc[key] || 0) + 1
            return acc
        }, {
            active: 0,
            starting: 0,
            suspended: 0,
            stopped: 0,
            unknown: 0,
        })

        const active = counts.active
        const starting = counts.starting
        const suspended = counts.suspended
        const stopped = counts.stopped

        if (total === 0) {
            return {
                active,
                starting,
                suspended,
                stopped,
                error: 0,
                idle: 0,
                total: 0,
                avgLoad: 0,
                avgCpu: 0,
                avgMemory: 0,
            }
        }

        const avgLoad = Math.round((active / total) * 100)
        const avgCpu = Math.round(
            nodes.reduce((sum, n) => sum + (Number(n.vcpu) || 0), 0) / total
        )
        const avgMemory = Math.round(
            nodes.reduce((sum, n) => sum + (Number(n.memory) || 0), 0) / total
        )

        // Keep legacy `error` and `idle` keys for existing UI components.
        return {
            active,
            starting,
            suspended,
            stopped,
            error: stopped,
            idle: suspended,
            total,
            avgLoad,
            avgCpu,
            avgMemory,
        }
    }, [nodes])
}
