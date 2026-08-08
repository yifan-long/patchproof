export const PROVIDER_STORAGE_KEY = 'patchproof.provider'

export function sanitizeStoredProvider(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return {
    base_url: typeof value.base_url === 'string' ? value.base_url : '',
    model: typeof value.model === 'string' ? value.model : '',
    transport: typeof value.transport === 'string' ? value.transport : 'auto'
  }
}

export function loadStoredProvider(storage) {
  const raw = storage.getItem(PROVIDER_STORAGE_KEY)
  if (!raw) return null
  try {
    const provider = sanitizeStoredProvider(JSON.parse(raw))
    if (provider) storage.setItem(PROVIDER_STORAGE_KEY, JSON.stringify(provider))
    return provider
  } catch {
    storage.removeItem(PROVIDER_STORAGE_KEY)
    return null
  }
}

export function saveStoredProvider(storage, provider) {
  const safeProvider = sanitizeStoredProvider(provider)
  if (safeProvider) storage.setItem(PROVIDER_STORAGE_KEY, JSON.stringify(safeProvider))
}
