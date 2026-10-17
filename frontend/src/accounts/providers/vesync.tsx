import { countryOptions, localTimeZone, timeZoneOptions } from '../../location-options'
import type { Account } from '../types'

export function VeSyncFields({ account }: { account: Account | null }) {
  return <>
    <label>Email<input data-testid="connection-email" name="email" type="email" autoComplete="username" required maxLength={254} defaultValue={account?.email} readOnly={!!account} /></label>
    <label>Password<input data-testid="connection-password" name="password" type="password" autoComplete="new-password" required maxLength={1024} /></label>
    <div className="form-columns">
      <label>VeSync account country<select data-testid="connection-country" name="country_code" required defaultValue={account?.country_code ?? 'US'}>
        {countryOptions(account?.country_code).map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <label>Time zone<select data-testid="connection-time-zone" name="time_zone" required defaultValue={account?.time_zone ?? localTimeZone}>
        {timeZoneOptions(account?.time_zone).map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
      </select></label>
    </div>
  </>
}

export function veSyncCredentials(form: FormData) {
  return Object.fromEntries(['email', 'password', 'country_code', 'time_zone'].map((key) => [key, form.get(key)]))
}
