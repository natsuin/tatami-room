// Tatami Room inside Claude Code. Every few seconds the mod asks `tatami mod poll` how this
// window's agent and its room are doing; the parts below draw from that. `tatami mod` runs as a
// child of this Claude process, which is how it finds the window's agent.
import { atom, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Snapshot } from '../types'
import { band } from './band'
import { guard } from './guard'
import { pane, ROOM_COMMAND } from './pane'
import { tatamiPath } from './room'
import { sawMail, status } from './status'
import { isBusy, turn } from './turn'

const EVERY_MS = 4000
const snap = atom({ plugin: 'tatami', key: 'snap' } as const, null)

let wake = true // the "Wake for tasks and messages" option
let polling = false
let last = ''
let tag: string | undefined

/** 'Furina, Nahida, Raiden Shogun, Yae Miko' -> 'Furina, Nahida +2': whole names, kept short. */
function people(names: string): string {
  const all = names.split(', ')
  const kept: string[] = []
  for (const one of all) {
    if (kept.length > 0 && [...kept, one].join(', ').length > 24) {
      break
    }
    kept.push(one.length > 24 ? `${one.slice(0, 23)}…` : one)
  }
  const rest = all.length - kept.length
  return kept.join(', ') + (rest > 0 ? ` +${rest}` : '')
}

/** The window's name tag on the status line: its desk name, who's in its picture, and its room. */
export function nameTag(s: Snapshot): string | undefined {
  if (!s.id) {
    return undefined // a session without the Tatami Room channel
  }
  const who = s.character ? ` · ${people(s.character)}` : ''
  return `${s.id}${who} · ${s.room ?? 'on its own'}`
}

async function poll($: EngineInterface, withWake: boolean): Promise<Snapshot | null> {
  const ran = await $.process.run([tatamiPath(await $.env.get('HOME')), 'mod', 'poll', ...(withWake ? ['--wake'] : [])],
    { timeoutMs: 10_000 })
  if (ran.exitCode !== 0) {
    return null // tatami isn't installed here
  }
  try {
    return JSON.parse(ran.stdout) as Snapshot
  } catch {
    return null
  }
}

async function tick($: EngineInterface) {
  if (polling) {
    return
  }
  polling = true
  try {
    const s = await poll($, wake && !isBusy()) // a working agent hears of room mail through the status hooks
    if (!s) {
      return
    }
    sawMail(s.unread)
    const now = nameTag(s)
    if (now !== tag) {
      tag = now
      $.ui.status(now)
    }
    const shown = JSON.stringify({ ...s, wake: null })
    if (shown !== last) {
      last = shown
      await update($, snap, () => ({ ...s, wake: null }))
    }
    if (s.wake) {
      void $.prompt.submit({ text: s.wake })
    }
  } finally {
    polling = false
  }
}

export const register: Register = (on, options) => {
  wake = options.wake !== false
  const glows = options.glow !== false

  on('session.start', async ($, e, next) => {
    try {
      await $.command.register(ROOM_COMMAND)
    } catch {
      // no slash commands here: the rest still works
    }
    void tick($)
    $.clock.every(EVERY_MS, () => void tick($))
    return next(e)
  })

  turn(on, glows)
  band(on, glows)
  pane(on)
  status(on, options.chime !== false)
  guard(on)
}
