/** @type {import('tailwindcss').Config} */
export default {
    content: ['./index.html', './src/**/*.{js,jsx}'],
    darkMode: 'class',
    theme: {
        extend: {
            colors: {
                void: {
                    900: '#050810',
                    800: '#0a0e1a',
                    700: '#111827',
                },
                neon: {
                    cyan: '#06b6d4',
                    indigo: '#6366f1',
                    purple: '#8b5cf6',
                    pink: '#ec4899',
                    rose: '#f43f5e',
                },
            },
            fontFamily: {
                sans: ['Inter', 'system-ui', 'sans-serif'],
                mono: ['"Geist Mono"', '"JetBrains Mono"', 'monospace'],
            },
            backdropBlur: {
                '2xl': '40px',
            },
        },
    },
    plugins: [],
}
