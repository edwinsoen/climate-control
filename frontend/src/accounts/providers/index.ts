import type { ComponentType } from 'react'
import { countryName } from '../../location-options'
import type { Account, ProviderId } from '../types'
import { VeSyncFields, veSyncCredentials } from './vesync'
import { XiaomiFields, xiaomiCredentials } from './xiaomi'

type ProviderDefinition = {
  id: ProviderId
  name: string
  /** Brand or service region shown under the provider name. */
  detail: string
  Fields: ComponentType<{ account: Account | null }>
  credentials: (form: FormData) => Record<string, unknown>
  region: (account: Account) => string
}

// Each provider owns its connection fields and request data.
export const providers: Record<ProviderId, ProviderDefinition> = {
  vesync: {
    id: 'vesync',
    name: 'VeSync',
    detail: 'Levoit',
    Fields: VeSyncFields,
    credentials: veSyncCredentials,
    region: (account) => `${countryName(account.country_code)} · ${account.time_zone}`,
  },
  xiaomi: {
    id: 'xiaomi',
    name: 'Xiaomi Home',
    detail: 'International cloud',
    Fields: XiaomiFields,
    credentials: xiaomiCredentials,
    region: (account) => `${account.country_code} server · ${account.time_zone}`,
  },
}

export const availableProviders = Object.values(providers)

export function getProvider(id: string): ProviderDefinition | undefined {
  return Object.hasOwn(providers, id) ? providers[id as ProviderId] : undefined
}
