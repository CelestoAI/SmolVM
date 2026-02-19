import React, { useRef, useEffect } from 'react'
import { useThree, useFrame } from '@react-three/fiber'
import * as THREE from 'three'
import { useViewStore } from '@/stores/useViewStore'

export default function InteractionManager({ meshRef, count }) {
    const { camera, mouse, raycaster } = useThree()
    const setSelectedVM = useViewStore((s) => s.setSelectedVM)
    const selectedVM = useViewStore((s) => s.selectedVM)

    // Helper to get instance ID from intersection
    const getIntersectedInfo = () => {
        if (!meshRef.current) return null
        raycaster.setFromCamera(mouse, camera)
        const intersects = raycaster.intersectObject(meshRef.current)
        if (intersects.length > 0) {
            return {
                instanceId: intersects[0].instanceId,
                point: intersects[0].point
            }
        }
        return null
    }

    // Handle clicks
    useEffect(() => {
        const handleClick = () => {
            const hit = getIntersectedInfo()
            if (hit) {
                // We know the index corresponds to the VM index
                // In a real app we'd map instanceId -> VM ID via the data array
                // But here they map 1:1
                const vmId = `vm-${String(hit.instanceId).padStart(4, '0')}`
                setSelectedVM(vmId)
                console.log('Selected VM:', vmId)
            } else {
                // Deselect if clicking empty space
                // setSelectedVM(null) // Optional: maybe we want to keep selection?
            }
        }

        window.addEventListener('click', handleClick)
        return () => window.removeEventListener('click', handleClick)
    }, [camera, mouse, raycaster, meshRef, setSelectedVM])

    // Visual feedback on hover (scale up)
    // Note: We can't easily change color per-frame efficiently without touching the buffer
    // So we might use a separate "highlight cursor" mesh or just pointer events
    useFrame(() => {
        document.body.style.cursor = 'default'
        const hit = getIntersectedInfo()
        if (hit) {
            document.body.style.cursor = 'pointer'
        }
    })

    return null
}
