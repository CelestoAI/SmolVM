import { create } from 'zustand'

export const useViewStore = create((set) => ({
    viewMode: 'bento', // 'hive' | 'bento'
    selectedVM: null,   // null | string (id)
    theme: localStorage.getItem('theme') || 'dark', // 'light' | 'dark'

    toggleTheme: () =>
        set((state) => {
            const nextTheme = state.theme === 'dark' ? 'light' : 'dark'
            localStorage.setItem('theme', nextTheme)
            return { theme: nextTheme }
        }),

    toggleView: () =>
        set((state) => ({
            viewMode: state.viewMode === 'hive' ? 'bento' : 'hive',
        })),

    setView: (mode) => set({ viewMode: mode }),

    setSelectedVM: (id) => set({ selectedVM: id }),
}))
