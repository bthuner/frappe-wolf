// Times arrive in UTC and are shown in the site's time zone (boot.time_zone), never the
// browser's: an operator abroad reads the same 02:00 as the team.

function parts(iso, timeZone, options) {
  return new Intl.DateTimeFormat('en-GB', { timeZone, ...options }).format(new Date(iso))
}

export function clock(iso, timeZone) {
  return iso ? parts(iso, timeZone, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }) : ''
}

export function day(iso, timeZone) {
  return iso ? parts(iso, timeZone, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }) : ''
}

export function full(iso, timeZone, { zone = true } = {}) {
  if (!iso) return ''
  const text = parts(iso, timeZone, {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  })
  return zone ? `${text} (${timeZone})` : text
}

export function duration(fromIso, toIso) {
  if (!fromIso || !toIso) return ''
  const minutes = Math.max(0, Math.round((new Date(toIso) - new Date(fromIso)) / 60000))
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours} h ${String(minutes % 60).padStart(2, '0')} min`
  return `${Math.floor(hours / 24)} d ${hours % 24} h`
}

// chart axis label in the site's zone: the clock for a day or less, else the date too
export function axisLabel(iso, timeZone, withDate) {
  const options = withDate
    ? { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
    : { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
  return parts(iso, timeZone, options)
}

export function ago(iso, now = new Date()) {
  return iso ? duration(iso, now.toISOString()) : ''
}
