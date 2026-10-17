export type AccountStatus = {
  id: string
  label: string
  status: string
  error: string | null
  last_success_at: string | null
}

export type VeSyncAccount = AccountStatus & {
  provider: 'vesync'
  email: string
  country_code: string
  time_zone: string
}

export type XiaomiAccount = AccountStatus & {
  provider: 'xiaomi'
  email: string
  country_code: string
  time_zone: string
}

export type Account = VeSyncAccount | XiaomiAccount
export type ProviderId = Account['provider']
