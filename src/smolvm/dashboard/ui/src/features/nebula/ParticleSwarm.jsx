import React, { useRef, useMemo, useEffect } from 'react'
import { useFrame } from '@react-three/fiber'
import * as THREE from 'three'
import { useViewStore } from '@/stores/useViewStore'

const dummy = new THREE.Object3D()
const tempColor = new THREE.Color()

// Forward ref so parent can access it for Raycasting
const ParticleSwarm = React.forwardRef(({ nodes, theme }, ref) => {
    // If ref not provided from parent, use internal one (though parent should provide it)
    const localRef = useRef()
    const meshRef = ref || localRef

    const count = nodes.length

    // Pre-compute stable per-particle data
    const particleData = useMemo(() => {
        const isDark = theme === 'dark'

        const activeColors = isDark ? [
            '#7dd3fc', // sky-300
            '#818cf8', // indigo-400
            '#06b6d4', // cyan-500
            '#67e8f9', // cyan-300
            '#a5b4fc', // indigo-300
            '#c4b5fd', // violet-300
        ] : [
            '#0284c7', // sky-600
            '#4f46e5', // indigo-600
            '#0891b2', // cyan-600
            '#7c3aed', // violet-600
            '#2563eb', // blue-600
        ]

        const bootingColor = isDark ? '#fcd34d' : '#d97706' // yellow-300 : amber-600
        const haltingColor = isDark ? '#fb923c' : '#ea580c' // orange-400 : orange-600
        const snapshotColor = isDark ? '#c084fc' : '#9333ea' // purple-400 : purple-600
        const errorColor = isDark ? '#f472b6' : '#e11d48' // pink-400 : rose-600
        const idleColor = isDark ? '#ffffff' : '#cbd5e1'   // TEMP DEBUG: White for visibility

        return nodes.map((node, i) => {
            let color
            let statusCode

            switch (node.status) {
                case 'active':
                    color = activeColors[i % activeColors.length]
                    statusCode = 0
                    break
                case 'booting':
                    color = bootingColor
                    statusCode = 2
                    break
                case 'halting':
                    color = haltingColor
                    statusCode = 3
                    break
                case 'snapshotting':
                    color = snapshotColor
                    statusCode = 4
                    break
                case 'error':
                    color = errorColor
                    statusCode = 1
                    break
                default: // idle
                    color = idleColor
                    statusCode = 5
            }

            return {
                x: node.x,
                y: node.y,
                z: node.z,
                phase: node.phase,
                status: statusCode,
                color,
                load: node.load,
            }
        })
    }, [nodes, theme])

    // Initialize colors once after mount
    useEffect(() => {
        const mesh = meshRef.current
        if (!mesh) return

        const isDark = theme === 'dark'

        for (let i = 0; i < count; i++) {
            const p = particleData[i]

            // Set initial position
            dummy.position.set(p.x, p.y, p.z)
            // Scale: Active(0)=1, Error(1)=1.4, Booting(2)=0.8, Others=0.5
            const scale = p.status === 0 ? 1 : p.status === 1 ? 1.4 : p.status === 2 ? 0.8 : 0.5
            dummy.scale.setScalar(scale)
            dummy.updateMatrix()
            mesh.setMatrixAt(i, dummy.matrix)

            // Set color
            if (p.color) {
                tempColor.set(p.color)
            } else {
                tempColor.set('#ffffff')
            }

            if (isDark) {
                // Base boost for visibility against dark void
                tempColor.multiplyScalar(1.5)
            }

            if (p.status === 0) { // Active
                const boost = 0.8 + (p.load / 100) * 1.5
                tempColor.multiplyScalar(boost)
            } else if (p.status === 1) { // Error
                tempColor.multiplyScalar(2.0)
            } else if (p.status === 2 || p.status === 4) { // Booting/Snapshotting
                tempColor.multiplyScalar(1.2)
            }
            mesh.setColorAt(i, tempColor)
        }

        mesh.instanceMatrix.needsUpdate = true
        if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true
    }, [particleData, count, meshRef, theme])

    const selectedVMId = useViewStore((s) => s.selectedVM)

    // Animate positions and selection state every frame
    useFrame((state) => {
        const mesh = meshRef.current
        if (!mesh) return
        const t = state.clock.elapsedTime

        // Get index of selected VM
        let selectedIndex = -1
        if (selectedVMId) {
            selectedIndex = parseInt(selectedVMId.split('-')[1])
        }

        for (let i = 0; i < count; i++) {
            const p = particleData[i]
            const phase = p.phase
            const isSelected = i === selectedIndex
            const hasSelection = selectedIndex !== -1

            // --- Position Logic ---
            if (p.status === 0) {
                // Active: gentle flowing drift
                dummy.position.set(
                    p.x + Math.sin(t * 0.3 + phase) * 0.08,
                    p.y + Math.sin(t * 0.6 + phase * 2) * 0.1,
                    p.z + Math.cos(t * 0.2 + phase) * 0.04
                )
            } else if (p.status === 1) {
                // Error: rapid jitter
                dummy.position.set(
                    p.x + (Math.random() - 0.5) * 0.07,
                    p.y + (Math.random() - 0.5) * 0.07,
                    p.z + (Math.random() - 0.5) * 0.05
                )
            } else if (p.status === 2) {
                // Booting: rising slightly
                dummy.position.set(
                    p.x + Math.sin(t * 0.1 + phase) * 0.02,
                    p.y + Math.sin(t * 0.5 + phase) * 0.05 + 0.1, // Offset up
                    p.z
                )
            } else {
                // Idle/Halting/Snapshotting: nearly still
                dummy.position.set(
                    p.x + Math.sin(t * 0.08 + phase) * 0.015,
                    p.y + Math.cos(t * 0.1 + phase) * 0.015,
                    p.z
                )
            }

            // --- Scale & Color Logic ---
            if (isSelected) {
                // Selected: Pulse size and turn bright white
                const pulse = 1 + Math.sin(t * 10) * 0.1
                dummy.scale.setScalar(2.5 * pulse)

                tempColor.set('#ffffff').multiplyScalar(10.0) // Super bright HDR white
                mesh.setColorAt(i, tempColor)
            } else {
                // Not selected
                let scale
                if (p.status === 0) scale = 0.85 + Math.sin(t * 1.0 + phase) * 0.2
                else if (p.status === 1) scale = 1.3 + Math.sin(t * 5 + phase) * 0.3
                else if (p.status === 2) scale = 0.7 + Math.sin(t * 2 + phase) * 0.1 // Booting pulse
                else scale = 0.45

                dummy.scale.setScalar(scale)

                const isDark = theme === 'dark'

                // Reset to base color (re-apply base logic)
                if (p.color) {
                    tempColor.set(p.color)
                } else {
                    tempColor.set('#ffffff')
                }

                if (isDark) {
                    tempColor.multiplyScalar(1.5)
                }

                if (hasSelection) {
                    // Dim others if something is selected
                    tempColor.multiplyScalar(0.3)
                } else {
                    // Normal state brightness
                    if (p.status === 0) { // Active
                        const boost = 0.8 + (p.load / 100) * 1.5
                        tempColor.multiplyScalar(boost)
                    } else if (p.status === 1) { // Error
                        tempColor.multiplyScalar(2.0)
                    } else if (p.status === 2 || p.status === 4) { // Booting/Snapshotting
                        tempColor.multiplyScalar(1.2) // Slight glow
                        if (p.status === 2) { // Booting pulse
                            const pulse = 1 + Math.sin(t * 3 + phase) * 0.3
                            tempColor.multiplyScalar(pulse)
                        }
                    }
                }
                mesh.setColorAt(i, tempColor)
            }

            dummy.updateMatrix()
            mesh.setMatrixAt(i, dummy.matrix)
        }

        mesh.instanceMatrix.needsUpdate = true
        if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true
    })

    return (
        <instancedMesh ref={meshRef} args={[null, null, count]} frustumCulled={false}>
            <sphereGeometry args={[0.04, 8, 8]} />
            <meshBasicMaterial vertexColors color="white" />
        </instancedMesh>
    )
})

export default ParticleSwarm
