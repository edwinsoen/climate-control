// ISO 3166-1 country codes; labels come from the browser's locale data.
const countryCodes = `AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP
KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY
MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY
QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ
VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW`.split(/\s+/)

const countryNames = new Intl.DisplayNames(['en'], { type: 'region' })

export function countryOptions(current = 'US') {
  return [...new Set([...countryCodes, current])]
    .map((value) => ({ value, label: countryNames.of(value) ?? value }))
    .sort((a, b) => a.label.localeCompare(b.label, 'en'))
}

export function countryName(code: string) {
  return countryNames.of(code) ?? code
}

export const localTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone

export function timeZoneOptions(current = localTimeZone) {
  // Include saved aliases and UTC, which supportedValuesOf can omit.
  return [...new Set([...Intl.supportedValuesOf('timeZone'), 'UTC', localTimeZone, current])]
    .sort()
    .map((value) => ({ value, label: value.replaceAll('_', ' ') }))
}
