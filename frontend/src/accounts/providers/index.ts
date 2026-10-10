import type { ComponentType } from 'react'
import type { Account, ProviderId } from '../types'
import { VeSyncFields, veSyncCredentials } from './vesync'

type ProviderDefinition = {
  id: ProviderId
  name: string
  Fields: ComponentType<{ account: Account | null }>
  credentials: (form: FormData) => Record<string, unknown>
}

// Each provider owns its connection fields and request data.
export const providers: Record<ProviderId, ProviderDefinition> = {
  vesync: {
    id: 'vesync',
    name: 'VeSync',
    Fields: VeSyncFields,
    credentials: veSyncCredentials,
  },
}

export const availableProviders = Object.values(providers)

export function getProvider(id: string): ProviderDefinition | undefined {
  return Object.hasOwn(providers, id) ? providers[id as ProviderId] : undefined
}
