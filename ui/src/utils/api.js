/**
 * Where the dashboard's API lives.
 *
 * Served from the dashboard itself, the API is on the same origin and a
 * relative path is enough. During development the UI runs on Vite's port 5173
 * while the server stays on 8000, so a relative path would ask Vite for the
 * API and get the page back instead. VITE_API_BASE_URL overrides both.
 */
export function getApiBaseUrl() {
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

/** Absolute URL for an API path such as `/api/command`. */
export function apiUrl(path) {
    return `${getApiBaseUrl()}${path}`
}
