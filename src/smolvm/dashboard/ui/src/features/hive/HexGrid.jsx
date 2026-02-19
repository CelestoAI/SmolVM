import React, { useRef, useState, useEffect, useMemo, useCallback } from 'react'
import HexNode from './HexNode'

const BASE_CELL_WIDTH = 104
const CELL_GAP = 8
const ROW_OVERLAP = 0
const ROW_GAP = 6
const ROW_SHIFT = (BASE_CELL_WIDTH + CELL_GAP) / 2
const INITIAL_VISIBLE_ROWS = 12
const ROW_BATCH_SIZE = 8

function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max)
}

export default function HexGrid({ nodes }) {
    const frameRef = useRef(null)
    const scrollerRef = useRef(null)
    const loaderRef = useRef(null)

    const [layout, setLayout] = useState({ scale: 1, columns: 8 })
    const [visibleRows, setVisibleRows] = useState(INITIAL_VISIBLE_ROWS)

    const updateLayout = useCallback(() => {
        if (!frameRef.current) return

        const width = frameRef.current.clientWidth
        const usableWidth = Math.max(width - 48, 320)
        const maxColumns = Math.max(
            2,
            Math.floor(usableWidth / (BASE_CELL_WIDTH + CELL_GAP))
        )
        const minColumns = usableWidth < 700 ? 2 : 4
        const columns = clamp(maxColumns, minColumns, 16)

        const widestRow = columns * (BASE_CELL_WIDTH + CELL_GAP) + ROW_SHIFT
        const minScale = usableWidth < 700 ? 0.72 : 0.84
        const scale = clamp(usableWidth / widestRow, minScale, 1)

        setLayout((prev) => {
            if (prev.columns === columns && Math.abs(prev.scale - scale) < 0.01) {
                return prev
            }
            return { columns, scale }
        })
    }, [])

    useEffect(() => {
        updateLayout()
        window.addEventListener('resize', updateLayout)
        return () => window.removeEventListener('resize', updateLayout)
    }, [updateLayout])

    // Organize nodes into honeycomb rows
    const rows = useMemo(() => {
        if (nodes.length === 0) return []

        const result = []
        let cursor = 0
        while (cursor < nodes.length) {
            const count = Math.max(layout.columns, 1)
            const rowNodes = nodes.slice(cursor, cursor + count)
            if (!rowNodes.length) break
            result.push(rowNodes)
            cursor += count
        }

        return result
    }, [nodes, layout.columns])

    useEffect(() => {
        if (rows.length === 0) {
            setVisibleRows(0)
            return
        }
        setVisibleRows(Math.min(INITIAL_VISIBLE_ROWS, rows.length))
    }, [rows.length, layout.columns])

    const loadMoreRows = useCallback(() => {
        setVisibleRows((prev) => Math.min(prev + ROW_BATCH_SIZE, rows.length))
    }, [rows.length])

    useEffect(() => {
        const root = scrollerRef.current
        const sentinel = loaderRef.current

        if (!root || !sentinel || visibleRows >= rows.length) return
        if (typeof window === 'undefined' || !('IntersectionObserver' in window)) {
            setVisibleRows(rows.length)
            return
        }

        const observer = new IntersectionObserver(
            (entries) => {
                if (entries[0]?.isIntersecting) {
                    loadMoreRows()
                }
            },
            {
                root,
                rootMargin: '260px 0px',
                threshold: 0.01,
            }
        )

        observer.observe(sentinel)
        return () => observer.disconnect()
    }, [loadMoreRows, rows.length, visibleRows])

    const displayRows = rows.slice(0, visibleRows)
    const hasMore = visibleRows < rows.length
    const compactNodes = layout.scale < 0.86
    const widestVisibleRow = displayRows.reduce(
        (max, row) => Math.max(max, row.length),
        0
    )
    const effectiveColumns = Math.max(widestVisibleRow, 1)
    const gridWidth = effectiveColumns * (BASE_CELL_WIDTH + CELL_GAP) + ROW_SHIFT

    return (
        <div ref={frameRef} className="relative w-full h-full overflow-hidden">
            <div className="absolute inset-0 hive-surface" />
            <div
                ref={scrollerRef}
                className="absolute inset-0 overflow-y-auto overflow-x-hidden hive-scroll pt-28 sm:pt-32 pb-24"
            >
                <div className="mx-auto w-full max-w-[1900px] px-3 sm:px-6 lg:px-10">
                    {rows.length === 0 ? (
                        <div className="py-20 text-center text-sm font-mono tracking-[0.2em] uppercase text-amber-900/70 dark:text-amber-100/40">
                            No nodes available
                        </div>
                    ) : (
                        <>
                            <div className="flex justify-center">
                                <div
                                    className="honeycomb-grid flex flex-col items-start transition-transform duration-300 ease-out"
                                    style={{
                                        transform: `scale(${layout.scale})`,
                                        transformOrigin: 'top center',
                                        width: `${gridWidth}px`,
                                    }}
                                >
                                    {displayRows.map((row, rIndex) => (
                                        <div
                                            key={`row-${rIndex}`}
                                            className="flex"
                                            style={{
                                                marginTop: rIndex === 0 ? 0 : `${ROW_GAP - ROW_OVERLAP}px`,
                                                paddingLeft: rIndex % 2 === 0 ? 0 : `${ROW_SHIFT}px`,
                                            }}
                                        >
                                            {row.map((node) => (
                                                <div key={node.id} className="mx-1">
                                                    <HexNode node={node} isCompact={compactNodes} />
                                                </div>
                                            ))}
                                        </div>
                                    ))}

                                    {hasMore && <div ref={loaderRef} className="h-16 w-full" />}
                                </div>
                            </div>

                            {hasMore && (
                                <div className="pb-10 pt-4 text-center text-[10px] font-mono tracking-[0.2em] uppercase text-amber-900/60 dark:text-amber-100/35">
                                    Loading cluster slices...
                                </div>
                            )}
                        </>
                    )}
                </div>
            </div>
        </div>
    )
}
