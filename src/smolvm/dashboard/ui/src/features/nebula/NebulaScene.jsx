import React, { Suspense, useRef } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { EffectComposer, Bloom } from '@react-three/postprocessing'
import ParticleSwarm from './ParticleSwarm'
import InteractionManager from './InteractionManager'

// Inner scene component (inside Canvas context)
function SceneContent({ nodes, theme }) {
    const groupRef = useRef()
    const meshRef = useRef() // Shared ref for raycasting

    // Theme-based environment colors
    const bg = theme === 'dark' ? '#050810' : '#f8fafc'
    const fogColor = theme === 'dark' ? '#050810' : '#f8fafc'
    const bloomIntensity = theme === 'dark' ? 1.4 : 0.4

    // Slow auto-rotation of the entire swarm
    useFrame(() => {
        if (groupRef.current) {
            groupRef.current.rotation.y += 0.0003
        }
    })

    return (
        <>
            <color attach="background" args={[bg]} />
            {/* <fog attach="fog" args={[fogColor, 8, 20]} /> */}

            <group ref={groupRef}>
                <ParticleSwarm ref={meshRef} nodes={nodes} theme={theme} />
                <InteractionManager meshRef={meshRef} count={nodes.length} />
            </group>

            <EffectComposer>
                <Bloom
                    luminanceThreshold={0.05}
                    luminanceSmoothing={0.9}
                    intensity={bloomIntensity}
                    mipmapBlur
                />
            </EffectComposer>
        </>
    )
}

export default function NebulaScene({ nodes, theme }) {
    return (
        <div style={{ position: 'absolute', inset: 0 }}>
            {/* 3D Scene */}
            <Canvas
                camera={{ position: [0, 2, 8], fov: 60, near: 0.1, far: 50 }}
                gl={{ antialias: false, alpha: false }}
                dpr={[1, 1.5]}
            >
                <Suspense fallback={null}>
                    <SceneContent nodes={nodes} theme={theme} />
                </Suspense>
            </Canvas>
        </div>
    )
}
