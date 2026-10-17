import { localTimeZone, timeZoneOptions } from '../../location-options'
import type { Account } from '../types'

const XIAOMI_SERVERS = [
  { value: 'SG', label: 'Singapore (SG)' },
  { value: 'DE', label: 'Europe (DE)' },
  { value: 'US', label: 'United States (US)' },
  { value: 'IN', label: 'India (IN)' },
  { value: 'RU', label: 'Russia (RU)' },
  { value: 'TW', label: 'Taiwan (TW)' },
]

export function XiaomiFields({ account }: { account: Account | null }) {
  const defaultServer = account?.country_code && XIAOMI_SERVERS.some((s) => s.value === account.country_code)
    ? account.country_code
    : 'SG'
  const needsVerificationCode = Boolean(account?.error?.includes('verification code'))
  const requireVerificationCode = Boolean(
    needsVerificationCode && !account?.error?.includes('invalid or expired')
  )
  return <>
    <label>Xiaomi account (ID, phone, or email)<input data-testid="connection-email" name="email" type="text" autoComplete="username" required maxLength={254} defaultValue={account?.email} readOnly={!!account} /></label>
    <label>Password{needsVerificationCode ? ' (optional if unchanged)' : ''}<input data-testid="connection-password" name="password" type="password" autoComplete="new-password" required={!needsVerificationCode} maxLength={1024} /></label>
    {account && <label>Verification code{needsVerificationCode ? '' : ' (if requested)'}<input data-testid="connection-verification-code" name="verification_code" type="text" inputMode="numeric" autoComplete="one-time-code" required={requireVerificationCode} autoFocus={needsVerificationCode} maxLength={32} placeholder="6-digit code from Xiaomi" /></label>}
    <div className="form-columns">
      <label>Xiaomi Cloud server<select data-testid="connection-country" name="country_code" required defaultValue={defaultServer}>
        {XIAOMI_SERVERS.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <label>Time zone<select data-testid="connection-time-zone" name="time_zone" required defaultValue={account?.time_zone ?? localTimeZone}>
        {timeZoneOptions(account?.time_zone).map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
      </select></label>
    </div>
  </>
}

export function xiaomiCredentials(form: FormData) {
  return Object.fromEntries(
    ['email', 'password', 'verification_code', 'country_code', 'time_zone'].map((key) => [key, form.get(key) ?? ''])
  )
}
