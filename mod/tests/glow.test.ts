import type { On, RenderElement } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'

import { AKA, SKY, took } from '../hooks/turn'
import type { Snapshot } from '../types'

const QUIET: Snapshot = { id: 'claude-grape', color: 'grape', character: null, room: 'sakura', roomColor: '#ffafd1',
  unread: 0, latest: null, messages: [], members: [], wake: null }

const START = { cwd: '/home/test', surface: 'terminal', isInteractive: true } as const

const BAND = {
  plugin: 'tatami', component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 12, bodyColumns: 100, scroll: { offset: 0, bodyRows: 12 }, view: {} },
} as const

// The engine beneath the mod: just enough of it for the glows' calls. `events` collects what the
// desk is told (`tatami mod event`).
function engine(on: On, snap: () => Snapshot = () => QUIET, events: string[] = []) {
  const clock = mock.clock(on)
  mock.env(on, { HOME: '/home/test' })
  on('process.run', async (_$, e) => {
    if (e.argv[2] === 'event') {
      events.push((JSON.parse(e.init?.stdin ?? '{}') as { hook_event_name: string }).hook_event_name)
    }
    const stdout = e.argv[2] === 'poll' ? JSON.stringify(snap()) : '{}'
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('session.start', async (_$, e) => ({ cwd: e.cwd }))
  on('command.register', async (_$, e) => ({ value: { command: e.name } }))
  on('ui.status', async () => ({ value: undefined }))
  on('turn.start', async (_$, e) => ({ turnId: e.turnId }))
  on('turn.complete', async (_$, e) => ({ text: e.answer }))
  on('classic.Notification', async () => ({}))
  on('classic.PostToolUse', async () => ({}))
  // What the engine draws where the mod passes: a tool's own row; nothing above the prompt.
  on('ui.render', async ($r, e) => {
    const { Text } = $r.ui.resolve(e)
    return h(Text, {}, e.component === 'ToolUse' ? '● Bash(npm install)' : '') as RenderElement
  })
  return clock
}

const DONE = { answer: 'done', durationMs: 75_000, isAborted: false, turnId: 't1', reason: 'answer' } as const

test('took reads like a short duration', async () => {
  expect(took(4_200)).toBe('4s')
  expect(took(75_000)).toBe('1m 15s')
  expect(took(0)).toBe('1s')
})

test('when Claude finishes, a sky-blue box breathes above the prompt, rests, and goes when the next turn starts', async ($, on) => {
  const clock = engine(on)
  await $.session.start(START)
  await clock.advance(0)
  await $.turn.start({ text: 'fix the bug', turnId: 't1' })
  await $.turn.complete(DONE)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    const border = async () => (await ui.findAll({ type: 'Box' })).find(b => b.props.borderStyle === 'round')?.props.borderColor
    expect(await ui.find({ type: 'Text', text: '● Your turn' })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Claude finished in 1m 15s/ })).toBeDefined()
    const shades = new Set([await border()])
    for (let i = 0; i < 4; i++) {
      await clock.advance(560)
      shades.add(await border())
    }
    expect(shades.size).toBeGreaterThan(2) // it breathes
    expect([...shades].every(s => SKY.includes(s as string))).toBe(true)
    await ui.unmount()
  }

  await clock.advance(31_000)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const border = async () => (await ui.findAll({ type: 'Box' })).find(b => b.props.borderStyle === 'round')?.props.borderColor
  expect(await border()).toBe(SKY[0])
  await clock.advance(3_000)
  expect(await border()).toBe(SKY[0]) // still there, resting

  await $.turn.start({ text: 'thanks', turnId: 't2' })
  expect(await ui.find({ type: 'Text', text: '● Your turn' })).toBeUndefined()
  await ui.unmount()
})

test('the box sits above room mail, and a turn you stopped yourself shows none', async ($, on) => {
  const clock = engine(on, () => ({ ...QUIET, unread: 1, latest: { from: 'claude-sky', text: 'tests pass' } }))
  await $.session.start(START)
  await clock.advance(0)
  await $.turn.start({ text: 'fix the bug', turnId: 't1' })
  await $.turn.complete(DONE)

  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  const turn = texts.findIndex(t => t === '● Your turn')
  const mail = texts.findIndex(t => /1 unread/.test(t ?? ''))
  expect(turn).toBeGreaterThanOrEqual(0)
  expect(mail).toBeGreaterThan(turn)
  await ui.unmount()

  await $.turn.start({ text: 'one more thing', turnId: 't2' })
  await $.turn.complete({ ...DONE, isAborted: true, reason: 'aborted', turnId: 't2' })
  const after = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await after.find({ type: 'Text', text: '● Your turn' })).toBeUndefined()
  expect(await after.find({ type: 'Text', text: /1 unread/ })).toBeDefined()
  await after.unmount()
})

// The engine holds a tool call at its permission prompt until `answer` lets it go.
function prompt(on: On) {
  const held = { id: '', answer: () => {} }
  on('tool.call', async (_$, e) => new Promise(resolve => {
    held.id = e.tool_use_id ?? ''
    held.answer = () => resolve({ result: { stdout: '', stderr: '', interrupted: false } })
  }))
  return held
}

// A call that asks for your OK. While it waits, the engine draws its row not running; the
// moment you say yes, running.
async function asked($: Parameters<TestBody>[0], held: ReturnType<typeof prompt>) {
  const call = $.tool.call({ tool: 'Bash', command: 'npm install' })
  for (let i = 0; i < 100 && held.id === ''; i++) {
    await Promise.resolve()
  }
  await $.classic.Notification({ notification_type: 'permission_prompt', message: 'Claude needs your permission to use Bash' })
  const id = held.id
  const props = { tool_use_id: id, tool: 'Bash', input: { command: 'npm install' }, isRunning: false, isErrored: false, isInterrupted: false }
  const row = await $.ui.mount({ plugin: 'tatami', surface: 'terminal', component: 'ToolUse', requestId: id, props })
  return { row, props, call, answer: () => held.answer() }
}

const FRAME = { type: 'Text', text: '● Needs your OK' } as const

test('a tool waiting for your OK pulses red, quicker, until you say yes, and the desk hears it then', async ($, on) => {
  const events: string[] = []
  const clock = engine(on, () => QUIET, events)
  const held = prompt(on)
  await $.session.start(START)
  await clock.advance(0)
  await $.turn.start({ text: 'install it', turnId: 't1' })
  const { row, props, call, answer } = await asked($, held)

  const border = async () => (await row.findAll({ type: 'Box' })).find(b => b.props.borderStyle === 'bold')?.props.borderColor
  expect(await row.find(FRAME)).toBeDefined()
  expect(await row.find({ type: 'Text', text: '● Bash(npm install)' })).toBeDefined() // the engine's own row, inside
  const shades = new Set([await border()])
  for (let i = 0; i < 3; i++) {
    await clock.advance(275)
    shades.add(await border())
  }
  expect(shades.size).toBe(3)
  expect([...shades].every(s => AKA.includes(s as string))).toBe(true)
  expect(events).not.toContain('PermissionGranted')

  // Yes: the tool starts, and may run for minutes. The frame goes now, and the desk hears it
  // within a beat, long before the tool is done.
  await row.redraw({ ...props, isRunning: true })
  expect(await row.find(FRAME)).toBeUndefined()
  await clock.advance(275)
  expect(events.filter(e => e === 'PermissionGranted')).toEqual(['PermissionGranted'])
  await clock.advance(10_000)
  await row.redraw({ ...props, isRunning: true })
  expect(await row.find(FRAME)).toBeUndefined()
  expect(events.filter(e => e === 'PermissionGranted').length).toBe(1)

  answer()
  await call
  expect(await row.find(FRAME)).toBeUndefined()
  await row.unmount()
})

test('a tool you refuse loses its frame without the desk hearing a yes', async ($, on) => {
  const events: string[] = []
  const clock = engine(on, () => QUIET, events)
  const held = prompt(on)
  await $.session.start(START)
  await clock.advance(0)
  await $.turn.start({ text: 'install it', turnId: 't1' })
  const { row, call, answer } = await asked($, held)
  expect(await row.find(FRAME)).toBeDefined()

  answer() // No: the call ends without its row ever being drawn running
  await call
  await clock.advance(1_000)
  expect(await row.find(FRAME)).toBeUndefined()
  expect(events).not.toContain('PermissionGranted')
  await row.unmount()
})

test('with the glow option off there is no frame, but the desk still hears your yes', { options: { glow: false } }, async ($, on) => {
  const events: string[] = []
  const clock = engine(on, () => QUIET, events)
  const held = prompt(on)
  await $.session.start(START)
  await clock.advance(0)
  await $.turn.start({ text: 'install it', turnId: 't1' })
  const { row, props, call, answer } = await asked($, held)
  expect(await row.find(FRAME)).toBeUndefined()

  await row.redraw({ ...props, isRunning: true })
  await clock.advance(275)
  expect(events).toContain('PermissionGranted')
  answer()
  await call
  await row.unmount()
})

test('with the glow option off, neither shows', { options: { glow: false } }, async ($, on) => {
  const clock = engine(on)
  await $.session.start(START)
  await clock.advance(0)
  await $.turn.start({ text: 'fix the bug', turnId: 't1' })
  await $.turn.complete(DONE)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: '● Your turn' })).toBeUndefined()
  await ui.unmount()
})
