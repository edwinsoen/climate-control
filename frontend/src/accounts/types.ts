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

// Extend this union when another provider is implemented.
export type Account = VeSyncAccount
export type ProviderId = Account['provider']
