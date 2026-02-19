import React from 'react'
import { useViewStore } from '@/stores/useViewStore'
import { motion, AnimatePresence } from 'framer-motion'
import { Sun, Moon } from 'lucide-react'

export default function ThemeToggle() {
    const theme = useViewStore((s) => s.theme)
    const toggleTheme = useViewStore((s) => s.toggleTheme)

    const isDark = theme === 'dark'

    return (
        <button
            onClick={toggleTheme}
            className={`
                relative h-8 w-16 rounded-full p-1 transition-colors duration-500 ease-in-out cursor-pointer
                ${isDark ? 'bg-slate-800 border border-slate-700 shadow-inner' : 'bg-slate-200 border border-slate-300 shadow-inner'}
            `}
            title={`Switch to ${isDark ? 'Light' : 'Dark'} Mode`}
            aria-label="Toggle Dark Mode"
        >
            {/* Track Icons (Inactive State) */}
            <div className="absolute inset-0 flex justify-between items-center px-2 pointer-events-none">
                <Sun
                    size={14}
                    className={`transition-opacity duration-300 ${isDark ? 'opacity-40 text-slate-400' : 'opacity-0'}`}
                />
                <Moon
                    size={14}
                    className={`transition-opacity duration-300 ${isDark ? 'opacity-0' : 'opacity-40 text-slate-500'}`}
                />
            </div>

            {/* Moving Thumb */}
            <motion.div
                className={`
                    absolute top-1 left-1
                    h-6 w-6 rounded-full shadow-md flex items-center justify-center
                    ${isDark ? 'bg-slate-700' : 'bg-gradient-to-br from-amber-300 to-orange-400'}
                `}
                animate={{
                    x: isDark ? 32 : 0,
                    backgroundColor: isDark ? '#334155' : '#fbbf24' // fallback colors
                }}
                transition={{ type: "spring", stiffness: 400, damping: 25 }}
            >
                <AnimatePresence mode="wait">
                    {isDark ? (
                        <motion.div
                            key="moon"
                            initial={{ scale: 0, rotate: -90, opacity: 0 }}
                            animate={{ scale: 1, rotate: 0, opacity: 1 }}
                            exit={{ scale: 0, rotate: 90, opacity: 0 }}
                            transition={{ duration: 0.2 }}
                        >
                            <Moon size={14} className="text-slate-200 fill-slate-200" />
                        </motion.div>
                    ) : (
                        <motion.div
                            key="sun"
                            initial={{ scale: 0, rotate: 90, opacity: 0 }}
                            animate={{ scale: 1, rotate: 0, opacity: 1 }}
                            exit={{ scale: 0, rotate: -90, opacity: 0 }}
                            transition={{ duration: 0.2 }}
                        >
                            <Sun size={14} className="text-white fill-white" />
                        </motion.div>
                    )}
                </AnimatePresence>
            </motion.div>
        </button>
    )
}
