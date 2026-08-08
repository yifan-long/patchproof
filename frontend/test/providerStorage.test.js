import test from 'node:test'
import assert from 'node:assert/strict'

import { loadStoredProvider, PROVIDER_STORAGE_KEY, saveStoredProvider } from '../src/providerStorage.js'

function memoryStorage(initialValue = null) {
  let value = initialValue
  return {
    getItem: () => value,
    setItem: (_key, nextValue) => { value = nextValue },
    removeItem: () => { value = null },
    value: () => value
  }
}

test('saveStoredProvider never persists an API key', () => {
  const storage = memoryStorage()
  saveStoredProvider(storage, {
    base_url: 'https://example.test/v1',
    model: 'test-model',
    transport: 'openai-compatible',
    api_key: 'must-not-persist'
  })

  assert.deepEqual(JSON.parse(storage.value()), {
    base_url: 'https://example.test/v1',
    model: 'test-model',
    transport: 'openai-compatible'
  })
  assert.equal(storage.value().includes('must-not-persist'), false)
})

test('loadStoredProvider strips a legacy API key and rewrites storage', () => {
  const storage = memoryStorage(JSON.stringify({
    base_url: 'https://example.test/v1',
    model: 'test-model',
    transport: 'openai-compatible',
    api_key: 'legacy-secret'
  }))

  const loaded = loadStoredProvider(storage)
  assert.equal(loaded.api_key, undefined)
  assert.equal(storage.value().includes('legacy-secret'), false)
  assert.deepEqual(JSON.parse(storage.value()), loaded)
})
