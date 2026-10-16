// Whose turn it is. Claude's while it works, which the poll needs to know (room mail reaches a
// working agent through the status hooks instead of a wake-up). Yours once it finishes, or when
// it asks before running a tool: then, with the "glow" option on, Claude's own window shows the
// desk's two glows. A box above the prompt says it's your turn, its border breathing slowly in
// the desk's sky blue for half a minute, then holding still until the next turn starts (band.tsx
// draws it). The row of a tool waiting for your OK gets a red frame that pulses quickly until you
// answer. A terminal can't blur a glow, so a border steps through lighter and darker shades.
// Once you say yes, the frame goes and the desk hears it's working, however long the tool runs.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, On, Timer } from 'claude-code'

import type { Finished } from '../types'
import { answered } from './status'

export const SKY = ['#8ecbff', '#a9d8ff', '#c6e6ff', '#a9d8ff', '#8ecbff', '#6faee6', '#5193cc', '#6faee6']
export const AKA = ['#ff4d5e', '#ff8792', '#ff4d5e', '#b8222f']
const SKY_BEAT_MS = 560 // all eight shades in about 4.5s, the desk's "your turn"
const AKA_BEAT_MS = 275 // all four in about 1.1s, the desk's "needs your OK"
const BREATHE_MS = 30_000

const finished = atom({ plugin: 'tatami', key: 'finished' } as const, null)
const asking = atom({ plugin: 'tatami', key: 'asking' } as const, '') // the tool call waiting for your OK
const beat = atom({ plugin: 'tatami', key: 'beat' } as const, 0)

let busy = false
// What the beat goes by, kept here as well as in the atoms: a hook that has waited a long time
// (a tool call waiting for your OK) can read an atom as it was when the hook started.
let running = '' // the newest tool call under way: the one a permission prompt is about
let waitingOn = '' // the one waiting for your OK
let started = '' // the one whose row was drawn running: you said yes (a drawing can't write, so the beat acts)
let breatheUntil = 0
let beating: Timer | null = null

/** A turn is running. */
export function isBusy(): boolean {
  return busy
}

/** 75_000 -> '1m 15s', 4_200 -> '4s' */
export function took(ms: number): string {
  const s = Math.max(1, Math.round(ms / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
}

// Starts, speeds up, slows down or stops the beat to fit what's showing.
async function pace($: EngineInterface) {
  beating?.cancel()
  beating = null
  await update($, beat, () => 0)
  const ms = waitingOn !== '' ? AKA_BEAT_MS : breatheUntil > (await $.clock.now()) ? SKY_BEAT_MS : 0
  if (ms > 0) {
    beating = $.clock.every(ms, () => void step($))
  }
}

async function step($: EngineInterface) {
  if (waitingOn !== '' && started === waitingOn) {
    await setWaiting($, '') // you said yes and it runs: the frame goes
    await answered($) // and the desk's card says it's working again
    return
  }
  if (waitingOn === '' && breatheUntil <= (await $.clock.now())) {
    return pace($) // done breathing: hold its own colour
  }
  await update($, beat, n => n + 1)
}

async function setFinished($: EngineInterface, next: Finished | null) {
  breatheUntil = next?.breatheUntil ?? 0
  await update($, finished, () => next)
  await pace($)
}

async function setWaiting($: EngineInterface, id: string) {
  waitingOn = id
  await update($, asking, () => id)
  await pace($)
}

export function turn(on: On, glows: boolean) {
  // A prompt you sent, or one that wakes it for room mail: the turn is Claude's again.
  on('turn.start', async ($, e, next) => {
    busy = true
    if (glows && (await read($, finished)) !== null) {
      await setFinished($, null)
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    busy = false
    if (e.agentId !== undefined) {
      return next(e) // a subagent's turn ends inside Claude's own
    }
    if (waitingOn !== '') {
      await setWaiting($, '')
    }
    if (!glows) {
      return next(e)
    }
    if (e.isAborted) {
      await setFinished($, null) // you stopped it yourself: nothing to point out
    } else {
      await setFinished($, { text: `Claude finished in ${took(e.durationMs)}`, breatheUntil: (await $.clock.now()) + BREATHE_MS })
    }
    return next(e)
  })

  // Who's waiting for your OK is followed with the glow option off too: the desk needs to hear
  // when you answer.
  on('tool.call', async ($, e, next) => {
    const id = e.tool_use_id ?? ''
    running = id
    try {
      return await next(e)
    } finally {
      if (id !== '' && waitingOn === id) {
        await setWaiting($, '') // refused, or run before the beat saw it start: the frame goes
      }
    }
  })

  on('classic.Notification', { notification_type: 'permission_prompt' }, async ($, e, next) => {
    if (running !== '') {
      await setWaiting($, running)
    }
    return next(e)
  })

  // The waiting tool's own row, framed. It stays in sight just above the permission dialog,
  // which hides the band above the prompt. The engine draws the row again, running, as soon as
  // you say yes: then it's drawn as it is, and the next beat (a quarter second at most) clears
  // the rest.
  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const id = await read($, asking)
    if (id === '' || e.props.tool_use_id !== id) {
      return next(e)
    }
    if (e.props.isRunning) {
      started = id
      return next(e)
    }
    if (!glows) {
      return next(e)
    }
    const { Box, Text } = $.ui.resolve(e)
    const shade = AKA[(await read($, beat)) % AKA.length]

    return (
      <Box flexDirection="column" borderStyle="bold" borderColor={shade} paddingX={1}>
        <Text color={shade} bold>● Needs your OK</Text>
        {await next(e)}
      </Box>
    )
  })
}
