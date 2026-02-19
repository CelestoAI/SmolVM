import React from 'react'
import DashboardLayout from '@/components/layout/DashboardLayout'
import { useSwarmData, useSwarmStats } from '@/hooks/useSwarmData'

export default function App() {
    const nodes = useSwarmData()
    const stats = useSwarmStats(nodes)

    return <DashboardLayout nodes={nodes} stats={stats} />
}
