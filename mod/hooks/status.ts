// Desk status without touching Claude Code's settings: the same four hook events `tatami hooks on`
// would add, run from the mod. Each goes to `tatami mod event` (hooks.py underneath), which notes
// whether the agent is working, done or waiting for your OK (the board's badges and the app's
// notifications read that), and hands back room mail to tell the agent about. With "Chime when
// Claude needs your OK" on, it also plays a chime on Windows when the agent starts waiting.
import type { EngineInterface, On } from 'claude-code'

import { tatamiPath } from './room'

/** What hooks.py answers, in the shape a settings hook prints. */
type HookOut = {
  decision?: 'block'
  reason?: string
  hookSpecificOutput?: { additionalContext?: string }
}

let state = '' // what the last event noted: a tool call that changes nothing needn't ask again
let mail = false // the agent has unread room mail (set by the poll), so a tool call may hear of it

export function sawMail(unread: number) {
  mail = unread > 0
}

async function record($: EngineInterface, e: object, chime: boolean): Promise<HookOut> {
  const ran = await $.process.run([tatamiPath(await $.env.get('HOME')), 'mod', 'event', ...(chime ? ['--chime'] : [])],
    { stdin: JSON.stringify(e), timeoutMs: 10_000 })
  if (ran.exitCode !== 0) {
    return {}
  }
  try {
    return JSON.parse(ran.stdout) as HookOut
  } catch {
    return {}
  }
}

function told(out: HookOut): string[] {
  const text = out.hookSpecificOutput?.additionalContext
  return text ? [text] : []
}

/** You said yes to the tool call it asked about, and it runs (turn.tsx sees it start): the desk
 * hears it's working now, not once the tool finishes, minutes later for a long one. */
export async function answered($: EngineInterface) {
  await record($, { hook_event_name: 'PermissionGranted' }, false)
  state = 'working'
}

export function status(on: On, chime: boolean) {
  on('classic.UserPromptSubmit', async ($, e, next) => {
    const r = await next(e)
    const out = await record($, e, false)
    state = 'working'
    const more = told(out)
    return more.length ? { ...r, additionalContext: [...(r.additionalContext ?? []), ...more] } : r
  })

  on('classic.PostToolUse', async ($, e, next) => {
    const r = await next(e)
    if (state === 'working' && !mail) {
      return r // most tool calls: nothing to note, nothing to tell
    }
    const out = await record($, e, false)
    state = 'working'
    const more = told(out)
    return more.length ? { ...r, additionalContext: [...(r.additionalContext ?? []), ...more] } : r
  })

  on('classic.Stop', async ($, e, next) => {
    const r = await next(e)
    const out = await record($, e, false)
    if (out.decision === 'block' && out.reason && !r.block) {
      state = 'working' // a message to it came in: it reads the room before it finishes
      return { ...r, block: out.reason }
    }
    state = 'done'
    return r
  })

  on('classic.Notification', async ($, e, next) => {
    const r = await next(e)
    await record($, e, chime)
    state = '' // asking, or done: the next tool call notes "working" again
    return r
  })
}
